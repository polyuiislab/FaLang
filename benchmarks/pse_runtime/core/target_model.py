import json
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class InstructionKey:
    opcode: str
    func: str


@dataclass(frozen=True)
class OperationKey:
    opcode: str
    func: str
    operator: str


# 异常类型号：写入 `.exctab` 的 8 位 exc_code。
# 目标机模型可在 `runtime_environment.exception_codes` 中覆盖；
# 此处只是模型缺省时的回退表，保证旧模型仍可编译。
UNKNOWN_EXCEPTION_CODE = 0xFF
DEFAULT_EXCEPTION_CODES: Dict[str, int] = {
    "ResourceFault": 0x01,
    "MaterialMissing": 0x02,
    "Timeout": 0x03,
    "QualityFault": 0x04,
    "DeviceOverheat": 0x05,
    "MachineFailure": 0x06,
    "OperatorUnavailable": 0x07,
    "ToolingDeviation": 0x08,
    "BufferUnavailable": 0x09,
    "LogisticsDepleted": 0x0A,
}


class TargetModelLoader:
    """
    Loads and queries a FaLang factory target model JSON.

    This loader supports:
    - Legacy schema (minimal fields) used by earlier demos
    - New schema described in `F_IDE_Project/target_model.md`
    """

    # FaLang IR op_type -> (opcode, func) mapping (dimension 1)
    _OPTYPE_TO_KEY: Dict[str, InstructionKey] = {
        "Process": InstructionKey("000001", "001"),
        "Move": InstructionKey("000001", "010"),
        "Inspect": InstructionKey("000001", "011"),
        "Adjust": InstructionKey("000001", "100"),
        "Load": InstructionKey("100101", "000"),
        "Store": InstructionKey("101101", "000"),
        "Assign": InstructionKey("000001", "010"),  # 与 Move 共用同一套编码
    }

    def __init__(self, model_path: str = None):
        if model_path is None:
            model_path = os.path.join(os.path.dirname(__file__), "factory_target_model.json")
        self.model_path = model_path
        self.data: Dict[str, Any] = {}

        # New-schema indexed views
        self._supported_instructions: Dict[InstructionKey, Dict[str, Any]] = {}
        self._rp_desc_to_code: Dict[Tuple[str, str, str], str] = {}
        self._rp_code_to_desc: Dict[Tuple[str, str, str], str] = {}
        self._palu_caps: Dict[int, List[OperationKey]] = {}
        self._catch_triggers: Dict[str, List[OperationKey]] = {}
        self._exception_codes: Dict[str, int] = {}
        self._processing_times: Dict[OperationKey, float] = {}
        self._setup_matrices: Dict[str, Dict[Tuple[str, str], float]] = {}
        self._standard_logistics_times: Dict[Tuple[str, str], float] = {}
        self._storage_type_map: Dict[int, str] = {}
        self._storage_index_map: Dict[int, int] = {}
        self._logistics_matrix: List[List[float]] = []

        # Legacy bits (still exposed for existing callers)
        self.resources: Dict[str, dict] = {}

    def load(self) -> Tuple[bool, str]:
        if not os.path.exists(self.model_path):
            return False, f"Model file not found: {self.model_path}"

        try:
            with open(self.model_path, "r", encoding="utf-8") as f:
                self.data = json.load(f) or {}
        except Exception as e:
            return False, f"Failed to parse target model: {str(e)}"

        # Build indices (new schema if present)
        self._index_new_schema()
        # Best-effort legacy indexing for older demos
        self._index_legacy_schema()
        return True, "Target model loaded successfully."

    # -------------------------
    # Indexing helpers
    # -------------------------
    def _index_new_schema(self) -> None:
        self._supported_instructions = {}
        self._rp_desc_to_code = {}
        self._rp_code_to_desc = {}
        self._palu_caps = {}
        self._catch_triggers = {}
        self._exception_codes = {}
        self._processing_times = {}
        self._storage_index_map = {}
        self._logistics_matrix = []

        ics = (self.data.get("instruction_capability_set") or {}) if isinstance(self.data, dict) else {}
        supported = ics.get("supported_instructions")
        if isinstance(supported, list):
            for inst in supported:
                if not isinstance(inst, dict):
                    continue
                opcode = str(inst.get("opcode") or "")
                func = str(inst.get("func") or "")
                if not opcode or not func:
                    continue
                key = InstructionKey(opcode, func)
                self._supported_instructions[key] = inst
                ops = inst.get("supported_rp") or []
                if isinstance(ops, list):
                    for o in ops:
                        if not isinstance(o, dict):
                            continue
                        rp = str(o.get("rp") or "")
                        desc = str(o.get("description") or "")
                        if not rp or not desc:
                            continue
                        self._rp_desc_to_code[(opcode, func, desc)] = rp
                        self._rp_code_to_desc[(opcode, func, rp)] = desc
                        for alias in o.get("aliases") or []:
                            if alias:
                                self._rp_desc_to_code[(opcode, func, str(alias))] = rp

        palus = ics.get("palu_definitions")
        if isinstance(palus, list):
            for p in palus:
                if not isinstance(p, dict):
                    continue
                palu_id = p.get("palu_id")
                if not isinstance(palu_id, int):
                    continue
                caps = []
                for c in p.get("supported_capabilities") or []:
                    if not isinstance(c, dict):
                        continue
                    opcode = str(c.get("opcode") or "")
                    func = str(c.get("func") or "")
                    rp = str(c.get("rp") or "")
                    if opcode and func and rp:
                        caps.append(OperationKey(opcode, func, rp))
                self._palu_caps[palu_id] = caps

        runtime = self.data.get("runtime_environment") or {}
        catch_triggers = runtime.get("catch_triggers")
        if isinstance(catch_triggers, list):
            for trig in catch_triggers:
                if not isinstance(trig, dict):
                    continue
                exc = str(trig.get("exception_type") or "")
                if not exc:
                    continue
                ops = []
                for op in trig.get("trigger_operations") or []:
                    if not isinstance(op, dict):
                        continue
                    opcode = str(op.get("opcode") or "")
                    func = str(op.get("func") or "")
                    rp = str(op.get("rp") or "")
                    if opcode and func and rp:
                        ops.append(OperationKey(opcode, func, rp))
                self._catch_triggers[exc] = ops

        codes = runtime.get("exception_codes")
        if isinstance(codes, dict):
            for name, value in codes.items():
                try:
                    self._exception_codes[str(name)] = int(value)
                except (TypeError, ValueError):
                    continue

        sched = self.data.get("scheduling_model") or {}
        ltm = sched.get("logistics_time_matrix") or {}
        storage_index_map = ltm.get("storage_index_map")
        if isinstance(storage_index_map, dict):
            for k, v in storage_index_map.items():
                try:
                    storage_id = int(k)
                except Exception:
                    continue
                if isinstance(v, int):
                    self._storage_index_map[storage_id] = v

        matrix = ltm.get("matrix")
        if isinstance(matrix, list):
            parsed = []
            for row in matrix:
                if not isinstance(row, list):
                    continue
                parsed.append([float(x) for x in row])
            self._logistics_matrix = parsed
        elif isinstance(sched.get("logistics_layout"), dict):
            self._generate_logistics_matrix_from_layout(sched.get("logistics_layout") or {})

        for pt in (sched.get("processing_times") or []):
            if not isinstance(pt, dict):
                continue
            opcode = str(pt.get("opcode") or "")
            func = str(pt.get("func") or "")
            rp = str(pt.get("rp") or "")
            if not opcode or not func or not rp:
                continue
            expected_time = pt.get("expected_time")
            if expected_time is None:
                continue
            try:
                t = float(expected_time)
            except Exception:
                continue
            self._processing_times[OperationKey(opcode, func, rp)] = t

        self._setup_matrices = {}
        setup_model = sched.get("setup_time_model") or {}
        for family in (setup_model.get("component_families") or []):
            if not isinstance(family, dict):
                continue
            name = str(family.get("name") or "")
            models = [str(m) for m in (family.get("models") or [])]
            matrix = family.get("matrix")
            if not name or not models or not isinstance(matrix, list):
                continue
            table: Dict[Tuple[str, str], float] = {}
            for i, src in enumerate(models):
                if i >= len(matrix) or not isinstance(matrix[i], list):
                    continue
                for j, dst in enumerate(models):
                    if j >= len(matrix[i]):
                        continue
                    try:
                        table[(src, dst)] = float(matrix[i][j])
                    except Exception:
                        continue
            self._setup_matrices[name] = table

        self._standard_logistics_times = {}
        for entry in (sched.get("standard_logistics_times") or []):
            if not isinstance(entry, dict):
                continue
            fl = str(entry.get("from_type") or "")
            tl = str(entry.get("to_type") or "")
            t = entry.get("time")
            if fl and tl and t is not None:
                try:
                    self._standard_logistics_times[(fl, tl)] = float(t)
                except Exception:
                    continue

        storage_model = self.data.get("storage_model") or {}
        for entry in (storage_model.get("capacity_constraints") or []):
            if not isinstance(entry, dict):
                continue
            sid = entry.get("storage_id")
            stype = str(entry.get("type") or "")
            if isinstance(sid, int) and stype:
                self._storage_type_map[sid] = stype

    def _generate_logistics_matrix_from_layout(self, layout: Dict[str, Any]) -> None:
        """Generate a storage-to-storage logistics matrix from MMS coordinates."""
        storage_points: Dict[int, Tuple[int, int, Optional[int]]] = {}

        warehouse = layout.get("warehouse") or {}
        warehouse_id = warehouse.get("storage_id", 100)
        warehouse_coord = warehouse.get("coord", [-1, -1])
        try:
            storage_points[int(warehouse_id)] = (int(warehouse_coord[0]), int(warehouse_coord[1]), None)
        except Exception:
            storage_points[100] = (-1, -1, None)

        for pm in layout.get("process_modules") or []:
            if not isinstance(pm, dict):
                continue
            coord = pm.get("coord") or [0, 0]
            ws_id = pm.get("workstation_id")
            try:
                x, y = int(coord[0]), int(coord[1])
            except Exception:
                x, y = 0, 0
            for reg_id in pm.get("register_ids") or []:
                try:
                    storage_points[int(reg_id)] = (x, y, int(ws_id))
                except Exception:
                    continue

        ids = sorted(storage_points.keys())
        self._storage_index_map = {sid: idx for idx, sid in enumerate(ids)}
        wh_base = float(layout.get("warehouse_base_time", 8.0))
        pm_base = float(layout.get("pm_base_time", 4.0))
        distance_factor = float(layout.get("distance_factor", 2.0))
        same_pm_time = float(layout.get("same_pm_time", 1.0))

        matrix: List[List[float]] = []
        for src in ids:
            sx, sy, sws = storage_points[src]
            row = []
            for dst in ids:
                dx, dy, dws = storage_points[dst]
                if src == dst:
                    row.append(0.0)
                    continue
                if sws is not None and sws == dws:
                    row.append(same_pm_time)
                    continue
                distance = abs(sx - dx) + abs(sy - dy)
                base = wh_base if src == int(warehouse_id) or dst == int(warehouse_id) else pm_base
                row.append(base + distance_factor * distance)
            matrix.append(row)
        self._logistics_matrix = matrix

    def _index_legacy_schema(self) -> None:
        self.resources = {}
        sched = self.data.get("scheduling_model") or {}
        workstations = sched.get("workstations")
        if not isinstance(workstations, list):
            return
        for ws in workstations:
            if not isinstance(ws, dict):
                continue
            ws_id = ws.get("id")
            local_alus = ws.get("local_alus")
            if not isinstance(ws_id, int):
                continue
            if isinstance(local_alus, int):
                local_alus = [local_alus]
            if not isinstance(local_alus, list):
                continue
            for alu_id in local_alus:
                if not isinstance(alu_id, int):
                    continue
                self.resources[f"ALU_{alu_id}"] = {"type": "PALU", "ws_id": ws_id, "id": alu_id}

    # -------------------------
    # Queries used by compiler
    # -------------------------
    def get_alu_info(self, alu_id: int) -> Optional[dict]:
        return self.resources.get(f"ALU_{alu_id}")

    def get_register_count(self) -> int:
        addr_model = self.data.get("addressing_model", {}) if isinstance(self.data, dict) else {}
        reg_file = addr_model.get("register_file", {}) if isinstance(addr_model, dict) else {}
        total = reg_file.get("total_count", 32)
        return int(total) if isinstance(total, int) else 32

    def optype_to_instruction(self, op_type: str) -> Optional[InstructionKey]:
        return self._OPTYPE_TO_KEY.get(op_type)

    def resolve_rp_code(self, opcode: str, func: str, rp_desc: Optional[str]) -> Optional[str]:
        """
        Contract: in FaLang source, `rp` is expressed by its `description` string.
        If rp_desc is None/empty, this returns "00000" when supported, else None.
        """
        # 1) Explicit description match (preferred)
        if rp_desc:
            code = self._rp_desc_to_code.get((opcode, func, rp_desc))
            if code:
                return code

            # Heuristic: allow "Desc;param=..." and take first segment as description.
            if ";" in rp_desc:
                head = rp_desc.split(";", 1)[0].strip()
                code = self._rp_desc_to_code.get((opcode, func, head))
                if code:
                    return code

        # 2) Default/fallbacks
        key = InstructionKey(opcode, func)
        inst = self._supported_instructions.get(key) or {}
        ops = inst.get("supported_rp") or []
        rp_codes = [str(o.get("rp")) for o in ops if isinstance(o, dict) and o.get("rp") is not None]

        # If only one rp exists, auto-select it for backwards compatibility.
        if len(rp_codes) == 1:
            return rp_codes[0]

        # Default rp for logistics ops is typically "00000" when present.
        if "00000" in rp_codes:
            return "00000"

        # Legacy-safe fallback when schema doesn't define rp at all.
        if not ops:
            return "00000"

        return None

    def is_rp_supported(self, opcode: str, func: str, rp_code: str) -> bool:
        key = InstructionKey(opcode, func)
        if key not in self._supported_instructions:
            return False
        inst = self._supported_instructions[key]
        ops = inst.get("supported_rp") or []
        if not isinstance(ops, list):
            return False
        return any(isinstance(o, dict) and str(o.get("rp") or "") == rp_code for o in ops)

    def any_palu_supports(self, opcode: str, func: str, operator_code: str) -> bool:
        opk = OperationKey(opcode, func, operator_code)
        for caps in self._palu_caps.values():
            if opk in caps:
                return True
        return False

    def triggers_exception(self, exception_type: str, opcode: str, func: str, operator_code: str) -> bool:
        ops = self._catch_triggers.get(exception_type) or []
        return OperationKey(opcode, func, operator_code) in ops

    def exception_types(self) -> List[str]:
        """本厂声明了触发条件的异常类型。"""
        return sorted(self._catch_triggers)

    def get_exception_code(self, exception_type: str) -> int:
        """
        异常类型号，写入 `.exctab`。

        类型号由目标机模型给出（`runtime_environment.exception_codes`），这样新增一类不确定性
        只需改模型，不需要动 codegen。模型未声明时退回内置默认表。
        """
        if exception_type in self._exception_codes:
            return self._exception_codes[exception_type]
        return DEFAULT_EXCEPTION_CODES.get(exception_type, UNKNOWN_EXCEPTION_CODE)

    def get_default_catch_deadline(self) -> float:
        runtime = self.data.get("runtime_environment") or {}
        try:
            return float(runtime.get("default_catch_deadline", 20.0))
        except Exception:
            return 20.0

    def get_min_catch_deadline(self) -> float:
        runtime = self.data.get("runtime_environment") or {}
        try:
            return float(runtime.get("min_catch_deadline", 1.0))
        except Exception:
            return 1.0

    def get_fault_release_latency(self) -> float:
        runtime = self.data.get("runtime_environment") or {}
        try:
            return float(runtime.get("fault_release_latency", 0.0))
        except Exception:
            return 0.0

    def get_processing_time(self, opcode: str, func: str, operator_code: str) -> Optional[float]:
        return self._processing_times.get(OperationKey(opcode, func, operator_code))

    def get_logistics_time(self, src_storage_id: int, dst_storage_id: int) -> Optional[float]:
        if src_storage_id not in self._storage_index_map or dst_storage_id not in self._storage_index_map:
            return None
        i = self._storage_index_map[src_storage_id]
        j = self._storage_index_map[dst_storage_id]
        if not self._logistics_matrix:
            return None
        if i < 0 or j < 0 or i >= len(self._logistics_matrix):
            return None
        row = self._logistics_matrix[i]
        if j >= len(row):
            return None
        return float(row[j])

    def get_expected_time(self, opcode: str, func: str, operator_code: str) -> Optional[float]:
        # For now we only reliably support processing_times without storage-id bindings.
        return self.get_processing_time(opcode, func, operator_code)

    def parse_operation_params(self, params: Optional[str]) -> Dict[str, str]:
        parsed: Dict[str, str] = {}
        if not params:
            return parsed
        for part in str(params).split(";"):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                parsed[key] = value
        return parsed

    def get_setup_time(self, previous_params: Optional[str], current_params: Optional[str]) -> float:
        previous = self.parse_operation_params(previous_params)
        current = self.parse_operation_params(current_params)
        family = current.get("component") or current.get("family") or current.get("component_family")
        if not family:
            return 0.0
        previous_family = previous.get("component") or previous.get("family") or previous.get("component_family")
        if previous_family and previous_family != family:
            return 0.0

        prev_model = previous.get("model") or previous.get("component_model")
        curr_model = current.get("model") or current.get("component_model")
        if not prev_model or not curr_model:
            return 0.0

        table = self._setup_matrices.get(family)
        if not table:
            return 0.0
        return float(table.get((prev_model, curr_model), 0.0))

    def get_standard_logistics_time(self, from_type: str, to_type: str) -> Optional[float]:
        return self._standard_logistics_times.get((from_type, to_type))

    def get_default_move_time(self) -> float:
        return self.get_standard_logistics_time("register", "register") or 1.0

    def get_default_load_time(self) -> float:
        return self.get_standard_logistics_time("memory", "register") or 20.0

    def get_default_store_time(self) -> float:
        return self.get_standard_logistics_time("register", "memory") or 20.0

    def get_storage_type(self, storage_id: int) -> Optional[str]:
        return self._storage_type_map.get(storage_id)

    def get_storage_regions(self) -> Dict[str, List[int]]:
        addr = (self.data.get("addressing_model") or {}).get("storage_regions", {})
        return {
            "memory_ids": list(addr.get("memory_ids") or []),
            "register_ids": list(addr.get("register_ids") or []),
        }
