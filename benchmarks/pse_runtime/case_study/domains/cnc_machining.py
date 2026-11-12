# F_IDE_Project/case_study/domains/cnc_machining.py
"""
Study 1 domain 之三 —— CNC 加工。

同样是 3×3 布局，但制造逻辑与前两个 domain 差别最大：单件加工时间长、工装与刀具偏置
是主要不确定性来源、换型由材料与特征组合决定。用来检验表示能力是否真的与门类无关。
"""
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

KEY = "cnc_machining"

ROLES = (
    ResourceRoleSpec("Milling_Center", "Resource", "Device", ("Process",)),
    ResourceRoleSpec("Turning_Center", "Resource", "Device", ("Process",)),
    ResourceRoleSpec("Finishing_Booth", "Resource", "Device", ("Process",)),
    ResourceRoleSpec("Inspection_Room", "Resource", "Device", ("Inspect", "Process")),
    ResourceRoleSpec("Recovery_PM", "Resource", "Device", ("Adjust",)),
    ResourceRoleSpec("Warehouse", "Storage", "Memory", ()),
)

ACTIONS = (
    ActionSpec("Load", "00000", "Stock_Store_To_Machine", 7.0),
    ActionSpec("Store", "00000", "Finished_Part_To_Store", 7.0),
    ActionSpec("Process", "00001", "Face_Mill", 14.0),
    ActionSpec("Process", "00010", "Drill_Holes", 11.0),
    ActionSpec("Process", "00011", "Pocket_Mill", 18.0),
    ActionSpec("Process", "00100", "Deburr", 6.0),
    ActionSpec("Process", "00101", "Surface_Treat", 12.0),
    ActionSpec("Process", "00110", "Pack_Part", 5.0),
    ActionSpec("Inspect", "00001", "CMM_Inspect", 10.0),
    ActionSpec("Inspect", "00010", "Surface_Check", 6.0),
    ActionSpec("Adjust", "00001", "Stock_Replenish", 9.0),
    ActionSpec("Adjust", "00010", "Tool_Offset_Adjust", 6.0),
    ActionSpec("Adjust", "00011", "Fixture_Reclamp", 5.0),
    ActionSpec("Adjust", "00100", "Rework_Cut", 12.0),
    ActionSpec("Adjust", "00101", "Operator_Reassign", 4.0),
)

RESOURCE_MODULES = (
    ResourceModuleSpec(
        palu_id=0,
        device_type="CNC_Mill",
        quantity=3,
        actions=(
            ("Process", "00001"),
            ("Process", "00010"),
            ("Process", "00011"),
            ("Process", "00100"),
        ),
    ),
    ResourceModuleSpec(
        palu_id=1,
        device_type="CNC_Lathe",
        quantity=2,
        actions=(("Process", "00001"), ("Process", "00011"), ("Process", "00100")),
    ),
    ResourceModuleSpec(
        palu_id=2,
        device_type="Finishing_Booth",
        quantity=1,
        actions=(("Process", "00101"), ("Process", "00110")),
    ),
    ResourceModuleSpec(
        palu_id=3,
        device_type="Transfer_AGV",
        quantity=4,
        actions=(("Move", "00000"), ("Load", "00000"), ("Store", "00000")),
    ),
    ResourceModuleSpec(
        palu_id=4,
        device_type="CMM_Station",
        quantity=2,
        actions=(("Inspect", "00001"), ("Inspect", "00010")),
    ),
    ResourceModuleSpec(
        palu_id=5,
        device_type="Setup_Technician",
        quantity=2,
        actions=(
            ("Adjust", "00001"),
            ("Adjust", "00010"),
            ("Adjust", "00011"),
            ("Adjust", "00100"),
            ("Adjust", "00101"),
        ),
    ),
)

PARADIGM = "flexible_job_shop"

# 柔性作业车间：按功能成组布置——铣削区 4 台、车削区 2 台、后处理区 2 台、检测区 1 台。
# 同一道工序可以在同组内任选一台设备；工序之间的先后由工艺决定，工件在功能区之间往返。
WORKSTATIONS = lay_out_workstations(
    rows=3,
    cols=3,
    slots=(
        # 铣削区（工位 0-3）
        ("milling", (0, 3)),
        ("milling", (0, 3)),
        ("milling", (0, 3)),
        ("milling", (0, 5)),
        # 车削区（工位 4-5）
        ("turning", (1, 3)),
        ("turning", (1, 5)),
        # 后处理区（工位 6-7）
        ("finishing", (2, 3, 5)),
        ("finishing", (2, 3, 5)),
        # 检测区（工位 8）
        ("inspection", (4, 3, 5)),
    ),
    slots_per_workstation=2,
    # 功能区成组：左列铣削、中列车削与后处理、右列检测
    coords=((0, 0), (0, 1), (0, 2), (1, 0), (1, 1), (1, 2), (2, 0), (2, 1), (2, 2)),
)

OPTION_FAMILIES = (
    OptionFamilySpec("Material", ("AL6061", "AL7075", "STEEL_1045", "BRASS"), setup_step=6.0),
    OptionFamilySpec("Feature", ("PLAIN", "HOLES", "POCKET"), setup_step=4.0),
    OptionFamilySpec("Tolerance", ("COARSE", "MEDIUM", "FINE"), setup_step=3.0),
    OptionFamilySpec("Coating", ("COATING_NONE", "ANODIZED", "NICKEL"), skip_model="COATING_NONE",
                     setup_step=5.0),
)

QUALITY_GRADES: Dict[str, tuple] = {
    "basic": ("Surface_Check",),
    "standard": ("Surface_Check", "CMM_Inspect"),
}

RULES = (
    ProcessRuleSpec("Inbound_Load", "stock", "Load", None, "Stock_Store_To_Machine"),
    ProcessRuleSpec("Face_Mill", "stock", "Process", "Milling_Center", "Face_Mill",
                    component="Material"),
    ProcessRuleSpec("Drill_Holes", "machining", "Process", "Milling_Center", "Drill_Holes",
                    component="Feature", skip_values=("PLAIN",)),
    ProcessRuleSpec("Pocket_Mill", "machining", "Process", "Turning_Center", "Pocket_Mill",
                    component="Feature", skip_values=("PLAIN", "HOLES")),
    ProcessRuleSpec("Deburr", "machining", "Process", "Milling_Center", "Deburr"),
    ProcessRuleSpec("Surface_Treat", "machining", "Process", "Finishing_Booth", "Surface_Treat",
                    component="Coating", skip_values=("COATING_NONE",)),
    ProcessRuleSpec("Surface_Check", "inspection", "Inspect", "Inspection_Room", "Surface_Check", grade_gated=True),
    ProcessRuleSpec("CMM_Inspect", "inspection", "Inspect", "Inspection_Room", "CMM_Inspect",
                    component="Tolerance", grade_gated=True),
    ProcessRuleSpec("Pack_Part", "inspection", "Process", "Inspection_Room", "Pack_Part"),
    ProcessRuleSpec("Outbound_Store", "inspection", "Store", None, "Finished_Part_To_Store"),
)

PROTECTIONS = (
    # 不锈钢与黄铜是长交期料，用到它们才预定义补料。
    ProtectionRuleSpec(
        region="stock",
        exception_type="MaterialMissing",
        uncertainty_key="material_shortage",
        when_component="Material", when_values=("STEEL_1045", "BRASS"),
        recovery=(("Load", None, "Stock_Store_To_Machine"),
                  ("Adjust", "Recovery_PM", "Stock_Replenish")),
        compensate=(("Adjust", "Recovery_PM", "Operator_Reassign"),),
    ),
    # 刀具偏置与夹具压紧是加工段的常态工况。
    ProtectionRuleSpec(
        region="machining",
        exception_type="ToolingDeviation",
        uncertainty_key="tooling_deviation",
        always=True,
        recovery=(("Adjust", "Recovery_PM", "Tool_Offset_Adjust"),
                  ("Adjust", "Recovery_PM", "Fixture_Reclamp")),
        compensate=(("Adjust", "Recovery_PM", "Tool_Offset_Adjust"),),
    ),
    # 铣削与车削互为替代，设备故障可把整段加工转到另一台同类设备上重做。
    ProtectionRuleSpec(
        region="machining",
        exception_type="MachineFailure",
        uncertainty_key="machine_failure",
        always=True,
        redispatch_region=True,
        compensate=(("Adjust", "Recovery_PM", "Operator_Reassign"),),
    ),
    # 只有高精度等级订单才做返修与复检。
    ProtectionRuleSpec(
        region="inspection",
        exception_type="QualityFault",
        uncertainty_key="quality_fault",
        when_grades=("standard",),
        recovery=(("Adjust", "Recovery_PM", "Rework_Cut"),
                  ("Inspect", "Inspection_Room", "CMM_Inspect")),
        compensate=(("Adjust", "Recovery_PM", "Rework_Cut"),),
    ),
)
CATCH_TRIGGERS = {
    # 触发条件必须覆盖**受保护区段里真实存在的工序**，否则那段保护不会产生任何异常边。
    "MaterialMissing": (("Load", "00000"),),
    "ToolingDeviation": (
        ("Process", "00010"),   # Drill_Holes
        ("Process", "00011"),   # Pocket_Mill
        ("Process", "00100"),   # Deburr
    ),
    "MachineFailure": (
        ("Process", "00010"),
        ("Process", "00011"),
        ("Process", "00101"),   # Surface_Treat
        ("Inspect", "00001"),
    ),
    "QualityFault": (("Inspect", "00001"), ("Inspect", "00010")),
}

SPEC = DomainSpec(
    key=KEY,
    label="Customized_CNC_Job_Shop",
    scenario="flexible job shop for customized CNC machining",
    material_decl="Material",
    material_name="Part_Order",
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
    arrival_mtba={"low": 26.0, "medium": 20.0, "high": 13.0},
)


def target_model() -> Dict[str, object]:
    return build_target_model(SPEC)
