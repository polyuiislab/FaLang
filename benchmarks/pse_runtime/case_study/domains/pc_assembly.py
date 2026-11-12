# F_IDE_Project/case_study/domains/pc_assembly.py
"""
Study 1 domain 之一 —— 定制化 PC 装配。

3×3 矩阵布局，9 个工位（比原 MMS 的 4×4 / 16 PM 小一档），每工位 2 个线边库位、
每个库位可放 2 件物料。订单由 4 个可配置组件族组合而成。
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

KEY = "pc_assembly"

ROLES = (
    ResourceRoleSpec("Assembly_PM", "Resource", "Device", ("Process",)),
    ResourceRoleSpec("Test_PM", "Resource", "Device", ("Inspect",)),
    ResourceRoleSpec("Packaging_PM", "Resource", "Device", ("Process", "Inspect")),
    ResourceRoleSpec("Recovery_PM", "Resource", "Device", ("Adjust",)),
    ResourceRoleSpec("Warehouse", "Storage", "Memory", ()),
)

ACTIONS = (
    # 领料与入库
    ActionSpec("Load", "00000", "Warehouse_To_Line_Side", 6.0),
    ActionSpec("Store", "00000", "Line_Side_To_Warehouse", 6.0),
    # 装配
    ActionSpec("Process", "00001", "Base_Assembly", 8.0),
    ActionSpec("Process", "00010", "CPU_Install", 7.0, aliases=("CPU_Mount",)),
    ActionSpec("Process", "00011", "Storage_Install", 5.0),
    ActionSpec("Process", "00100", "GPU_Install", 9.0),
    ActionSpec("Process", "00101", "Cooler_Install", 6.0),
    ActionSpec("Process", "00110", "Case_Assembly", 7.0),
    ActionSpec("Process", "01001", "Packaging", 5.0),
    # 检测
    ActionSpec("Inspect", "00001", "Power_On_Test", 6.0),
    ActionSpec("Inspect", "00010", "Benchmark_Test", 12.0),
    ActionSpec("Inspect", "00100", "Accessory_Check", 4.0),
    # 恢复
    ActionSpec("Adjust", "00001", "Material_Replenish", 8.0),
    ActionSpec("Adjust", "00010", "Tooling_Adjust", 5.0),
    ActionSpec("Adjust", "00011", "Quality_Rework", 10.0),
    ActionSpec("Adjust", "00100", "Operator_Reassign", 4.0),
)

RESOURCE_MODULES = (
    ResourceModuleSpec(
        palu_id=0,
        device_type="Assembly_Robot",
        quantity=5,
        actions=(
            ("Process", "00001"),
            ("Process", "00010"),
            ("Process", "00011"),
            ("Process", "00100"),
            ("Process", "00101"),
            ("Process", "00110"),
        ),
    ),
    ResourceModuleSpec(
        palu_id=1,
        device_type="Test_Bench",
        quantity=3,
        actions=(("Inspect", "00001"), ("Inspect", "00010")),
    ),
    ResourceModuleSpec(
        palu_id=2,
        device_type="Packaging_Station",
        quantity=2,
        actions=(("Process", "01001"), ("Inspect", "00100")),
    ),
    ResourceModuleSpec(
        palu_id=3,
        device_type="Logistics_AGV",
        quantity=6,
        actions=(("Move", "00000"), ("Load", "00000"), ("Store", "00000")),
    ),
    ResourceModuleSpec(
        palu_id=4,
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

# 行优先铺满 3×3：装配 ×5、测试 ×3、包装 ×1
WORKSTATIONS = lay_out_workstations(
    rows=3,
    cols=3,
    slots=(
        ("assembly", (0, 3)),
        ("assembly", (0, 3)),
        ("assembly", (0, 3)),
        ("assembly", (0, 3)),
        ("assembly", (0, 3)),
        ("testing", (1, 3, 4)),
        ("testing", (1, 3, 4)),
        ("testing", (1, 3, 4)),
        ("packaging", (2, 3, 4)),
    ),
    slots_per_workstation=2,
)

OPTION_FAMILIES = (
    OptionFamilySpec("CPU", ("CPU_1", "CPU_2", "CPU_3", "CPU_4"), setup_step=5.0),
    OptionFamilySpec("GPU", ("GPU_NONE", "GPU_1", "GPU_2", "GPU_3"), skip_model="GPU_NONE", setup_step=6.0),
    OptionFamilySpec("Storage", ("SSD_1", "SSD_2", "SSD_3"), setup_step=3.0),
    OptionFamilySpec("Cooling", ("COOLING_NONE", "COOLING_1", "COOLING_2"),
                     skip_model="COOLING_NONE", setup_step=3.0),
)

QUALITY_GRADES: Dict[str, tuple] = {
    "basic": ("Power_On_Test",),
    "standard": ("Power_On_Test", "Benchmark_Test"),
}

RULES = (
    ProcessRuleSpec("Inbound_Load", "assembly", "Load", None, "Warehouse_To_Line_Side"),
    ProcessRuleSpec("Base_Assembly", "assembly", "Process", "Assembly_PM", "Base_Assembly"),
    ProcessRuleSpec("CPU_Install", "assembly", "Process", "Assembly_PM", "CPU_Install",
                    component="CPU"),
    ProcessRuleSpec("Storage_Install", "assembly", "Process", "Assembly_PM", "Storage_Install",
                    component="Storage"),
    ProcessRuleSpec("GPU_Install", "assembly", "Process", "Assembly_PM", "GPU_Install",
                    component="GPU", skip_values=("GPU_NONE",)),
    ProcessRuleSpec("Cooler_Install", "assembly", "Process", "Assembly_PM", "Cooler_Install",
                    component="Cooling", skip_values=("COOLING_NONE",)),
    ProcessRuleSpec("Case_Assembly", "fixture", "Process", "Assembly_PM", "Case_Assembly",
                    component="CPU"),
    ProcessRuleSpec("Power_On_Test", "verification", "Inspect", "Test_PM", "Power_On_Test", grade_gated=True),
    ProcessRuleSpec("Benchmark_Test", "verification", "Inspect", "Test_PM", "Benchmark_Test", grade_gated=True),
    ProcessRuleSpec("Accessory_Check", "verification", "Inspect", "Packaging_PM", "Accessory_Check"),
    ProcessRuleSpec("Packaging", "verification", "Process", "Packaging_PM", "Packaging"),
    ProcessRuleSpec("Outbound_Store", "verification", "Store", None, "Line_Side_To_Warehouse"),
)

PROTECTIONS = (
    # 独显是长交期件，用到独显的订单才预定义缺料补料。
    ProtectionRuleSpec(
        region="assembly",
        exception_type="MaterialMissing",
        uncertainty_key="material_shortage",
        when_component="GPU", when_values=("GPU_1", "GPU_2", "GPU_3"),
        recovery=(("Load", None, "Warehouse_To_Line_Side"),
                  ("Adjust", "Recovery_PM", "Material_Replenish")),
        compensate=(("Adjust", "Recovery_PM", "Operator_Reassign"),),
    ),
    # 装外壳依赖工装定位，所有订单都预定义工装处置。
    ProtectionRuleSpec(
        region="fixture",
        exception_type="ToolingDeviation",
        uncertainty_key="tooling_deviation",
        always=True,
        recovery=(("Adjust", "Recovery_PM", "Tooling_Adjust"),),
        compensate=(("Adjust", "Recovery_PM", "Tooling_Adjust"),),
    ),
    # 上电检测的返工：检测不合格就返工并重检。
    ProtectionRuleSpec(
        region="verification",
        exception_type="QualityFault",
        uncertainty_key="quality_fault",
        always=True,
        recovery=(("Adjust", "Recovery_PM", "Quality_Rework"),
                  ("Inspect", "Test_PM", "Power_On_Test")),
        compensate=(("Adjust", "Recovery_PM", "Quality_Rework"),),
    ),
)
CATCH_TRIGGERS = {
    "MaterialMissing": (
        ("Load", "00000"),
        ("Process", "00010"),
        ("Process", "00011"),
        ("Process", "00100"),
        ("Process", "00101"),
    ),
    "ToolingDeviation": (
        ("Process", "00001"),
        ("Process", "00110"),
    ),
    "QualityFault": (
        ("Inspect", "00001"),
        ("Inspect", "00010"),
    ),
    "MachineFailure": (
        ("Process", "00001"),
        ("Process", "00110"),
        ("Inspect", "00001"),
    ),
}

SPEC = DomainSpec(
    key=KEY,
    label="Customized_PC_Assembly_Cell",
    scenario="3x3 matrix-structured customized PC assembly cell",
    material_decl="Material",
    material_name="PC_Order",
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
    arrival_mtba={"low": 22.0, "medium": 16.0, "high": 11.0},
)


def target_model() -> Dict[str, object]:
    return build_target_model(SPEC)
