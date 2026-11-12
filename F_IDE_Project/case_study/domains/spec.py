"""Domain specifications and conversion to the factory target-model schema."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Sequence, Tuple


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

# Exception identifiers are shared across domain models.
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
    """Map a FaLang operation to an instruction capability and nominal duration."""

    op_type: str
    rp: str
    description: str
    processing_time: float
    aliases: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ResourceModuleSpec:
    """A resource module and its supported instruction capabilities."""

    palu_id: int
    device_type: str
    quantity: int
    # Supported capabilities as (op_type, rp) pairs.
    actions: Tuple[Tuple[str, str], ...]
    normal_capacity_ratio: float = 1.0


@dataclass(frozen=True)
class WorkstationSpec:

    workstation_id: int
    ws_type: str
    row: int
    col: int
    registers: Tuple[int, ...]
    palus: Tuple[int, ...]


@dataclass(frozen=True)
class ProcessRuleSpec:
    """Select an operation and logical resource role from order attributes."""

    name: str
    region: str
    op_type: str
    role: Optional[str]
    rp: str
    component: Optional[str] = None
    skip_values: Tuple[str, ...] = ()
    grade_gated: bool = False


@dataclass(frozen=True)
class ProtectionRuleSpec:
    """Select a protected process region and response steps from order attributes."""

    region: str
    exception_type: str
    uncertainty_key: str
    always: bool = False
    when_component: Optional[str] = None
    when_values: Tuple[str, ...] = ()
    when_grades: Tuple[str, ...] = ()
    # Response steps as (op_type, role, rp) tuples.
    recovery: Tuple[Tuple[str, str, str], ...] = ()
    compensate: Tuple[Tuple[str, str, str], ...] = ()
    # Repeat the protected region on a compatible logical resource.
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
    """Configuration alternatives for a component or specification family."""

    family: str
    models: Tuple[str, ...]
    # Option values that omit the associated operation.
    skip_model: Optional[str] = None
    # Setup time: setup_step * abs(previous_option - next_option).
    setup_step: float = 0.0


@dataclass(frozen=True)
class ResourceRoleSpec:
    """A logical resource role and its required capabilities."""

    name: str
    decl_keyword: str
    category: str
    capabilities: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DomainSpec:

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
    # Exception type to triggering (op_type, rp) pairs.
    catch_triggers: Mapping[str, Tuple[Tuple[str, str], ...]]

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
        """Look up nominal duration by action code or description."""
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
    """Assign row-major slots or explicit coordinates used to derive transport distances."""
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


def build_target_model(spec: DomainSpec) -> Dict[str, object]:
    """Expand a domain specification into the target-model JSON schema."""
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

    # Include trigger mappings for the exception types used by generated responses.
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
