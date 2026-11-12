# F_IDE_Project/case_study/domains/spec.py
"""
Paper 1 Study 1 —— 跨 domain 的表示要素。

制造语境：Paper 1 的主张是"同一条订单意图可以落在不同工厂、不同制造门类上"。要检验这一点，
需要的不是一个通用模型，而是三份**彼此独立**的工厂描述：各自的工序动作、各自的设备、
各自的产能与布局、各自的工艺规则。三者之间共享的只有 FaLang 的语义核心与编译流水线。

本模块给出描述一个 manufacturing domain 所需的全部要素，并把这些要素展开成
`core/target_model.py` 能直接读取的 Factory Target Machine Model JSON。
展开出来的 JSON 会落盘保存，作为 Study 1 的直接证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


# 与 FaLang IR 的 op_type 对应
OP_PROCESS = ("000001", "001")
OP_MOVE = ("000001", "010")
OP_INSPECT = ("000001", "011")
OP_ADJUST = ("000001", "100")
OP_LOAD = ("100101", "000")
OP_STORE = ("101101", "000")

OPCODE_FUNC_BY_OP_TYPE: Dict[str, Tuple[str, str]] = {
    "Process": OP_PROCESS,
    "Move": OP_MOVE,
    "Inspect": OP_INSPECT,
    "Adjust": OP_ADJUST,
    "Load": OP_LOAD,
    "Store": OP_STORE,
}

OP_TYPE_BY_FUNC: Dict[Tuple[str, str], str] = {
    key: op_type for op_type, key in OPCODE_FUNC_BY_OP_TYPE.items()
}

# 异常类型号：三个 domain 共用同一张表，避免类型语义随 domain 漂移
EXCEPTION_CODES: Dict[str, int] = {
    "ResourceFault": 1,
    "MaterialMissing": 2,
    "Timeout": 3,
    "QualityFault": 4,
    "DeviceOverheat": 5,
    "MachineFailure": 6,
    "OperatorUnavailable": 7,
    "ToolingDeviation": 8,
    "BufferUnavailable": 9,
    "LogisticsDepleted": 10,
}


@dataclass(frozen=True)
class ActionSpec:
    """一条制造动作：FaLang 的 op_type 对应到目标机的一个 (opcode, func, rp)。"""

    op_type: str
    rp: str
    description: str
    processing_time: float
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceModuleSpec:
    """一类资源模块（一台设备/一名操作员/一辆物流车）。"""

    palu_id: int
    device_type: str
    quantity: int
    # 该模块能执行的动作，写成 (op_type, rp)
    actions: Tuple[Tuple[str, str], ...]
    normal_capacity_ratio: float = 1.0


@dataclass(frozen=True)
class WorkstationSpec:
    """一个工位：绑定的持有位置与资源模块。"""

    workstation_id: int
    ws_type: str
    row: int
    col: int
    registers: Tuple[int, ...]
    palus: Tuple[int, ...]


@dataclass(frozen=True)
class ProcessRuleSpec:
    """一条工艺规则：什么时候做哪道工序、由哪类逻辑资源角色做。"""

    name: str
    region: str
    op_type: str
    role: Optional[str]
    rp: str
    component: Optional[str] = None
    skip_values: Tuple[str, ...] = ()
    # 由订单质量等级决定做不做（检测工序）
    grade_gated: bool = False


@dataclass(frozen=True)
class ProtectionRuleSpec:
    """
    一条预定义保护：某段工序区段在什么条件下、针对哪类不确定性、按什么步骤处置。

    关键是 `when_*` 条件——**是否预定义由订单属性决定**，不是所有订单都写同一套 try/catch。
    例如用了长交期面料的订单才需要写"缺料时从仓库补料"，用了普通面料的订单不必写。
    """

    region: str
    exception_type: str
    uncertainty_key: str
    # 订单属性触发条件
    always: bool = False
    when_component: Optional[str] = None
    when_values: Tuple[str, ...] = ()
    when_grades: Tuple[str, ...] = ()
    # 处置步骤 (op_type, role, rp)
    recovery: Tuple[Tuple[str, str, str], ...] = ()
    compensate: Tuple[Tuple[str, str, str], ...] = ()
    # 处置是否为"把本区段的工序在同类逻辑资源上重做一遍"（用于设备故障的转移处置）
    redispatch_region: bool = False

    def applies_to(self, order) -> bool:
        if self.always:
            return True
        matched = False
        if self.when_grades:
            if getattr(order, "quality_grade", None) not in self.when_grades:
                return False
            matched = True
        if self.when_component:
            options = getattr(order, "components", None) or getattr(order, "options", {}) or {}
            value = options.get(self.when_component)
            if value is None or (self.when_values and value not in self.when_values):
                return False
            matched = True
        return matched


@dataclass(frozen=True)
class OptionFamilySpec:
    """一个可配置的组件/规格族，决定订单的可变形空间。"""

    family: str
    models: Tuple[str, ...]
    # 取该值时跳过对应工序（例如无独显、无领）
    skip_model: Optional[str] = None
    # 换型时间步长：s(i,j) = setup_step * |i-j|
    setup_step: float = 0.0


@dataclass(frozen=True)
class ResourceRoleSpec:
    """逻辑资源角色的声明（VMRS）。"""

    name: str
    decl_keyword: str
    category: str
    capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainSpec:
    """一个 manufacturing domain 的完整描述。"""

    key: str
    label: str
    scenario: str
    material_decl: str
    material_name: str

    roles: Tuple[ResourceRoleSpec, ...]
    actions: Tuple[ActionSpec, ...]
    resource_modules: Tuple[ResourceModuleSpec, ...]
    workstations: Tuple[WorkstationSpec, ...]

    option_families: Tuple[OptionFamilySpec, ...]
    quality_grades: Mapping[str, Tuple[str, ...]]

    rules: Tuple[ProcessRuleSpec, ...]
    protections: Tuple[ProtectionRuleSpec, ...]
    # 异常类型 -> 会触发它的 (op_type, rp)
    catch_triggers: Mapping[str, Tuple[Tuple[str, str], ...]]

    # 生产范式：matrix（矩阵式 MMS）/ flexible_assembly_line（柔性装配线）
    # / flexible_job_shop（柔性作业车间）
    paradigm: str = "matrix"
    rows: int = 3
    cols: int = 3
    holding_slots_per_workstation: int = 2
    holding_capacity: int = 2
    warehouse_capacity: int = 5000
    warehouse_base_time: float = 6.0
    pm_base_time: float = 3.0
    distance_factor: float = 2.0
    same_pm_time: float = 1.0
    default_catch_deadline: float = 20.0
    fault_release_latency: float = 3.0
    arrival_mtba: Mapping[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def register_ids(self) -> List[int]:
        return [r for ws in self.workstations for r in ws.registers]

    @property
    def logistics_module(self) -> Optional[ResourceModuleSpec]:
        for module in self.resource_modules:
            if any(op_type == "Move" for op_type, _ in module.actions):
                return module
        return None

    def role_names(self) -> Tuple[str, ...]:
        return tuple(role.name for role in self.roles)

    def rules_of_region(self, region: str) -> Tuple[ProcessRuleSpec, ...]:
        return tuple(rule for rule in self.rules if rule.region == region)

    def action_time(self, op_type: str, key: str) -> Optional[float]:
        """
        按动作编码或动作描述查预期处理时间。

        工艺规则里写的是动作描述（`Base_Assembly`），目标机模型里存的是编码（`00001`），
        两者都要能查到，否则订单工作量会被算成 0。
        """
        for action in self.actions:
            if action.op_type != op_type:
                continue
            if key in (action.rp, action.description) or key in action.aliases:
                return action.processing_time
        return None


def lay_out_workstations(
    rows: int,
    cols: int,
    slots: Sequence[Tuple[str, Tuple[int, ...]]],
    slots_per_workstation: int = 2,
    register_start: int = 0,
    coords: Optional[Sequence[Tuple[int, int]]] = None,
) -> Tuple[WorkstationSpec, ...]:
    """
    把工位铺到车间布局上。

    `slots` 是行优先的 (工位类型, 该工位可用的资源模块编号)；长度必须等于 rows*cols。
    `coords` 可显式指定每个工位在车间里的坐标（列, 行），用于表达不同的生产范式：

    - 矩阵式（MMS）：坐标即网格坐标，工位按类型分散布置，工件可在任意相容工位之间流转；
    - 流水线（flexible assembly line）：坐标排成一条线，同一工段的并行工位相邻，
      工件必须依次经过各工段；
    - 机群式（flexible job shop）：坐标按功能区域成组，同类设备聚在一起，
      工件按工序类型在不同区域之间往返。

    物流时间由坐标的曼哈顿距离导出，因此布局本身就是范式的一部分。
    """
    if len(slots) != rows * cols:
        raise ValueError(f"expected {rows * cols} workstation slots, got {len(slots)}")
    if coords is not None and len(coords) != len(slots):
        raise ValueError(f"expected {len(slots)} coordinates, got {len(coords)}")

    workstations: List[WorkstationSpec] = []
    next_register = register_start
    for index, (ws_type, palus) in enumerate(slots):
        if coords is not None:
            col, row = coords[index]
        else:
            row, col = divmod(index, cols)
        registers = tuple(range(next_register, next_register + slots_per_workstation))
        next_register += slots_per_workstation
        workstations.append(WorkstationSpec(
            workstation_id=index,
            ws_type=ws_type,
            row=row,
            col=col,
            registers=registers,
            palus=tuple(palus),
        ))
    return tuple(workstations)


# ----------------------------------------------------------------------
# 展开成 Factory Target Machine Model JSON
# ----------------------------------------------------------------------
def build_target_model(spec: DomainSpec) -> Dict[str, object]:
    """
    把 domain 描述展开成目标机模型。

    展开结果的 schema 与 `core/factory_target_model.json` 完全一致，
    因此同一个 `TargetModelLoader` 与同一条编译流水线可以直接读取，
    不需要为任何 domain 改一行编译器代码。
    """
    supported_by_key: Dict[Tuple[str, str], List[Dict[str, object]]] = {}
    processing_times: List[Dict[str, object]] = []
    for action in spec.actions:
        opcode, func = OPCODE_FUNC_BY_OP_TYPE[action.op_type]
        entry: Dict[str, object] = {"rp": action.rp, "description": action.description}
        if action.aliases:
            entry["aliases"] = list(action.aliases)
        supported_by_key.setdefault((opcode, func), []).append(entry)
        processing_times.append({
            "opcode": opcode,
            "func": func,
            "rp": action.rp,
            "expected_time": action.processing_time,
        })

    supported_instructions: List[Dict[str, object]] = []
    for op_type, (opcode, func) in OPCODE_FUNC_BY_OP_TYPE.items():
        entries = supported_by_key.get((opcode, func))
        if not entries:
            continue
        supported_instructions.append({
            "opcode": opcode,
            "func": func,
            "name": {"Process": "PROCESS", "Move": "MOVE", "Inspect": "INSPECT",
                     "Adjust": "ADJUST", "Load": "PLOAD", "Store": "PSTORE"}[op_type],
            "supported_rp": entries,
        })

    palu_definitions: List[Dict[str, object]] = []
    for module in spec.resource_modules:
        caps: List[Dict[str, str]] = []
        for op_type, rp in module.actions:
            opcode, func = OPCODE_FUNC_BY_OP_TYPE[op_type]
            caps.append({"opcode": opcode, "func": func, "rp": rp})
        definition: Dict[str, object] = {
            "palu_id": module.palu_id,
            "device_type": module.device_type,
            "quantity": module.quantity,
            "supported_capabilities": caps,
        }
        if module.normal_capacity_ratio < 1.0:
            definition["normal_capacity_ratio"] = module.normal_capacity_ratio
            definition["reserved_capacity_ratio"] = round(1.0 - module.normal_capacity_ratio, 3)
        palu_definitions.append(definition)

    capacity_constraints: List[Dict[str, object]] = []
    for ws in spec.workstations:
        for index, register in enumerate(ws.registers):
            role = "line_side_buffer" if index % 2 == 0 else "unloading_buffer"
            capacity_constraints.append({
                "storage_id": register,
                "type": "register",
                "role": role,
                "workstation_id": ws.workstation_id,
                "capacity": spec.holding_capacity,
            })
    warehouse_id = 100
    capacity_constraints.append({
        "storage_id": warehouse_id,
        "type": "memory",
        "role": "warehouse",
        "capacity": spec.warehouse_capacity,
    })

    process_modules = [
        {"workstation_id": ws.workstation_id, "pm_type": ws.ws_type,
         "coord": [ws.col, ws.row]}
        for ws in spec.workstations
    ]

    setup_families: List[Dict[str, object]] = []
    for family in spec.option_families:
        size = len(family.models)
        matrix = [
            [round(family.setup_step * abs(i - j), 3) for j in range(size)]
            for i in range(size)
        ]
        setup_families.append({
            "name": family.family,
            "models": list(family.models),
            "matrix": matrix,
        })

    # 只有**真的会被写进订单程序**的异常类型才需要触发条件：
    # 把预定义保护里用到的类型并进 catch_triggers，保证它一定会产生异常边。
    declared = dict(spec.catch_triggers)
    for protection in spec.protections:
        declared.setdefault(protection.exception_type, ())

    catch_triggers: List[Dict[str, object]] = []
    for exception_type, triggers in declared.items():
        ops: List[Dict[str, str]] = []
        for op_type, rp in triggers:
            opcode, func = OPCODE_FUNC_BY_OP_TYPE[op_type]
            ops.append({"opcode": opcode, "func": func, "rp": rp})
        catch_triggers.append({"exception_type": exception_type, "trigger_operations": ops})

    arrivals = dict(spec.arrival_mtba) or {"low": 24.0, "medium": 18.0, "high": 12.0}

    return {
        "meta": {
            "factory_name": spec.label,
            "version": "1.0",
            "scenario": spec.scenario,
            "domain": spec.key,
            "production_paradigm": spec.paradigm,
        },
        "case_study_model": {
            "product_customization": {family.family: len(family.models)
                                      for family in spec.option_families},
            "arrival_process": {
                "distribution": "poisson",
                "loads": [{"name": name, "mtba": value} for name, value in arrivals.items()],
                "orders_per_scenario": 0,
                "replications": 0,
            },
            "replanning_triggers": [
                "new_order_arrival", "buffer_blocking_released",
                "failed_device_repaired", "exception_raised",
            ],
        },
        "instruction_capability_set": {
            "supported_instructions": supported_instructions,
            "palu_definitions": palu_definitions,
        },
        "addressing_model": {
            "storage_regions": {
                "memory_ids": [warehouse_id],
                "register_ids": spec.register_ids,
            },
            "workstation_mapping": [
                {
                    "workstation_id": ws.workstation_id,
                    "pm_type": ws.ws_type,
                    "assigned_registers": list(ws.registers),
                    "assigned_palus": list(ws.palus),
                }
                for ws in spec.workstations
            ],
        },
        "storage_model": {"capacity_constraints": capacity_constraints},
        "runtime_environment": {
            "default_catch_deadline": spec.default_catch_deadline,
            "min_catch_deadline": 1.0,
            "fault_release_latency": spec.fault_release_latency,
            "catch_triggers": catch_triggers,
            "exception_codes": dict(EXCEPTION_CODES),
        },
        "scheduling_model": {
            "logistics_layout": {
                "warehouse": {"storage_id": warehouse_id, "coord": [-1, -1]},
                "warehouse_base_time": spec.warehouse_base_time,
                "pm_base_time": spec.pm_base_time,
                "distance_factor": spec.distance_factor,
                "same_pm_time": spec.same_pm_time,
                "process_modules": process_modules,
            },
            "processing_times": processing_times,
            "standard_logistics_times": [
                {"from_type": "memory", "to_type": "register", "time": 10.0},
                {"from_type": "register", "to_type": "memory", "time": 10.0},
                {"from_type": "register", "to_type": "register", "time": 5.0},
                {"from_type": "memory", "to_type": "memory", "time": 0.0},
            ],
            "setup_time_model": {"component_families": setup_families},
        },
    }
