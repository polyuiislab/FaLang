"""Schedule MDG operations within blocks under device, material-ownership, and storage-capacity constraints."""

import copy
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from compiler.ir import OpInstr, BasicBlock, ControlFlowGraph, BranchInstr, JumpInstr
from compiler.mdg import ManufacturingDependencyGraph, MDGNode, MDGEdge
from core.target_model import OperationKey


def _material_arg_indices(op_type: str, argc: int):

    if op_type == "Process":
        return range(1, argc)
    if op_type == "Inspect":
        return [1] if argc >= 2 else []
    if op_type in ("Move", "Load", "Store"):
        return [0] if argc >= 1 else []
    if op_type == "Assign":
        return [0] if argc >= 1 else []
    return []


def _is_versioned_var(name: str, resource_names: Set[str]) -> bool:
    if not isinstance(name, str) or not name:
        return False
    if name.isdigit() or name.startswith('"'):
        return False
    if name in resource_names:
        return False
    parts = name.rsplit('_', 1)
    return len(parts) == 2 and parts[1].isdigit()


@dataclass
class ResourceState:
    """Resource state propagated between scheduled blocks."""
    # PALU type to instance availability times.
    palu_instances: Dict[int, List[float]] = field(default_factory=dict)
    # Material version to (holder node, storage type, ready time).
    material_status: Dict[str, Tuple[Optional[int], Optional[str], float]] = field(default_factory=dict)
    # Idle material versions occupying each storage type.
    cache_occupancy: Dict[str, Set[str]] = field(default_factory=dict)
    # Aggregate capacity for each storage type.
    cache_capacity: Dict[str, int] = field(default_factory=dict)

    device_last_model: Dict[str, str] = field(default_factory=dict)


@dataclass
class ScheduleResult:

    block_schedules: Dict[int, List[Tuple[MDGNode, float, float]]]
    global_makespan: float
    device_timeline: Dict[str, List[Tuple[float, float, str]]]
    material_timeline: Dict[str, List[Tuple[float, float, str]]]
    cache_peak: Dict[str, int]
    mfr_boundaries: List[Tuple[int, ...]]
    exit_states: Dict[int, ResourceState]


class ListScheduler:


    def __init__(
        self,
        mdg: ManufacturingDependencyGraph,
        cfg: ControlFlowGraph,
        target_model=None,
        scheduling_tick: float = 0.0,
        alpha: float = 1.0,
        beta: float = 0.5,
    ):
        self.mdg = mdg
        self.cfg = cfg
        self.tm = target_model
        self.tick = scheduling_tick
        self.alpha = alpha
        self.beta = beta

        self.resource_names = set(getattr(cfg, "resource_names", set()) or set())
        self.material_names = set(getattr(cfg, "material_names", set()) or set())
        self.node_map = {n.id: n for n in mdg.nodes}


        self.cache_capacity = {}
        if self.tm and hasattr(self.tm, "data"):
            storage = (self.tm.data.get("storage_model") or {}).get("capacity_constraints", [])
            for entry in storage:
                stype = entry.get("type")
                cap = entry.get("capacity")
                if stype and cap is not None:
                    self.cache_capacity[stype] = self.cache_capacity.get(stype, 0) + int(cap)


        self.palu_capabilities: Dict[int, Set[OperationKey]] = {}
        self.palu_quantities: Dict[int, int] = {}
        self.palu_device_type: Dict[int, str] = {}
        self._build_palu_pool(target_model)


        self.initial_palu_instances: Dict[int, List[float]] = {}
        for pid, qty in self.palu_quantities.items():
            self.initial_palu_instances[pid] = [0.0] * qty


        self.cp_weight = self._compute_cp_weights()


        self._palu_last_model: Dict[int, str] = {}

        self._device_last_model: Dict[str, str] = {}
        self._ready_set: Set[MDGNode] = set()


        self._node_palu_allocation: Dict[int, Tuple[int, int]] = {}


    def _get_latency(self, node: MDGNode) -> float:
        if node.instr is None:
            return 0.0
        if isinstance(node.instr, OpInstr) and node.instr.expected_time is not None:
            return float(node.instr.expected_time)
        if self.tm and isinstance(node.instr, OpInstr):
            op_type = node.instr.op_type
            if op_type == "Load":
                return self.tm.get_default_load_time()
            if op_type == "Store":
                return self.tm.get_default_store_time()
            if op_type in ("Move", "Assign"):
                return self.tm.get_default_move_time()
        return 0.0

    def _get_device(self, node: MDGNode) -> Optional[str]:
        if not isinstance(node.instr, OpInstr):
            return None
        instr = node.instr
        if instr.op_type in ("Process", "Inspect", "Adjust"):
            if instr.args:
                dev = instr.args[0]
                if dev in self.resource_names:
                    return dev
        return None

    def _get_input_materials(self, node: MDGNode) -> List[str]:
        if not isinstance(node.instr, OpInstr):
            return []
        idxs = _material_arg_indices(node.instr.op_type, len(node.instr.args))
        mats = []
        for i in idxs:
            if i < len(node.instr.args):
                arg = node.instr.args[i]
                if _is_versioned_var(arg, self.resource_names):
                    mats.append(arg)
        return mats

    def _get_output_materials(self, node: MDGNode) -> List[str]:
        if not isinstance(node.instr, OpInstr):
            return []
        outs = []
        if node.instr.target and _is_versioned_var(node.instr.target, self.resource_names):
            outs.append(node.instr.target)
        for it in (node.instr.implicit_targets or []):
            if _is_versioned_var(it, self.resource_names):
                outs.append(it)
        return outs

    def _get_storage_types(self, node: MDGNode) -> Tuple[Optional[str], Optional[str]]:
        """Return source and destination storage types: memory, register, or None."""
        if not isinstance(node.instr, OpInstr):
            return None, None
        op_type = node.instr.op_type
        if op_type == "Load":
            return "memory", "register"
        if op_type == "Store":
            return "register", "memory"
        if op_type in ("Move", "Assign"):
            args = node.instr.args
            if len(args) >= 2:
                stype = self._resolve_storage_name_to_type(args[1])
                if stype:
                    return None, stype
            return "register", "register"
        return None, "register"

    def _resolve_storage_name_to_type(self, name: str) -> Optional[str]:
        if not isinstance(name, str):
            return None
        lower = name.lower()
        if self.tm and hasattr(self.tm, "data"):
            addr = (self.tm.data.get("addressing_model") or {}).get("storage_regions", {})
            for sid in addr.get("memory_ids", []):
                if str(sid) == name:
                    return "memory"
            for sid in addr.get("register_ids", []):
                if str(sid) == name:
                    return "register"
        if "warehouse" in lower or "finished" in lower:
            return "memory"
        if "staging" in lower or "wip" in lower or "buffer" in lower:
            return "register"
        return None


    def _build_palu_pool(self, target_model):

        if target_model is None or not hasattr(target_model, "data") or not target_model.data:
            return
        palus = (target_model.data.get("instruction_capability_set") or {}).get("palu_definitions", [])
        for palu in palus:
            if not isinstance(palu, dict):
                continue
            pid = palu.get("palu_id")
            if not isinstance(pid, int):
                continue
            qty = int(palu.get("quantity", 1))
            self.palu_quantities[pid] = qty
            self.palu_device_type[pid] = str(palu.get("device_type", f"PALU_{pid}"))
            caps: Set[OperationKey] = set()
            for c in (palu.get("supported_capabilities") or []):
                if not isinstance(c, dict):
                    continue
                opcode = str(c.get("opcode") or "")
                func = str(c.get("func") or "")
                rp = str(c.get("rp") or "")
                if opcode and func and rp:
                    caps.add(OperationKey(opcode, func, rp))
            self.palu_capabilities[pid] = caps

    def _get_compatible_palu_types(self, node: MDGNode) -> List[int]:

        if not isinstance(node.instr, OpInstr):
            return []
        instr = node.instr
        op_type = instr.op_type
        key = self.tm.optype_to_instruction(op_type) if self.tm else None
        if key is None:
            return []
        rp = instr.rp
        if not rp:
            return []
        target = OperationKey(key.opcode, key.func, rp)
        compatible: List[int] = []
        for pid, caps in self.palu_capabilities.items():
            if target in caps:
                compatible.append(pid)
        return compatible

    def _get_palu_capability(self, node: MDGNode) -> Optional[Tuple[str, str, str]]:

        if not isinstance(node.instr, OpInstr):
            return None
        instr = node.instr
        key = self.tm.optype_to_instruction(instr.op_type) if self.tm else None
        if key is None:
            return None
        rp = instr.rp
        if not rp:
            return None
        return (key.opcode, key.func, rp)

    def _find_best_palu_instance(
        self, node: MDGNode, state: ResourceState, current_time: float,
    ) -> Tuple[Optional[int], Optional[int], float]:
        """Return (PALU type, instance index, earliest availability time)."""
        compatible = self._get_compatible_palu_types(node)
        if not compatible:
            return None, None, 0.0

        best_pid: Optional[int] = None
        best_idx: Optional[int] = None
        best_busy: float = float('inf')

        for pid in compatible:
            instances = state.palu_instances.get(pid, [])
            for idx, busy_until in enumerate(instances):
                if busy_until <= current_time + 1e-9:

                    if busy_until < best_busy:
                        best_busy = busy_until
                        best_pid = pid
                        best_idx = idx
                else:

                    if busy_until < best_busy:
                        best_busy = busy_until

        return best_pid, best_idx, best_busy


    def _compute_cp_weights(self) -> Dict[int, float]:
        cp: Dict[int, float] = {}
        _computing: set = set()  # Bidirectional setup relations can introduce cycles into priority traversal.

        def dfs(node: MDGNode) -> float:
            nid = node.id
            if nid in cp:
                return cp[nid]
            if nid in _computing:

                return 0.0
            _computing.add(nid)
            max_w = 0.0
            for e in node.out_edges:
                if e.edge_type in ("data", "control", "struct", "exc", "setup"):
                    w = e.weight + dfs(e.dst)
                    if w > max_w:
                        max_w = w
            _computing.discard(nid)
            cp[nid] = max_w
            return max_w

        for node in self.mdg.nodes:
            dfs(node)
        return cp

    def _priority(self, node: MDGNode) -> float:
        cp = self.cp_weight.get(node.id, 0.0)
        setup_pressure = self._setup_pressure(node)
        scarcity = self._resource_scarcity(node)
        structure_hazard_bonus = self._structure_hazard_priority(node)

        return cp + self.alpha * setup_pressure + self.beta * scarcity + structure_hazard_bonus

    def _resource_scarcity(self, node: MDGNode) -> float:
        """Measure demand relative to the number of compatible PALU instances."""
        compatible = self._get_compatible_palu_types(node)
        if not compatible:
            return 0.0

        best_scarcity = 0.0
        for pid in compatible:
            count = sum(
                1 for n in self.mdg.nodes
                if n.node_type == "op" and pid in self._get_compatible_palu_types(n)
            )
            qty = self.palu_quantities.get(pid, 1)
            scarcity = float(count) / float(qty)
            if scarcity > best_scarcity:
                best_scarcity = scarcity
        return 10.0 * best_scarcity

    def _setup_pressure(self, node: MDGNode) -> float:
        """Return the maximum outgoing setup cost."""
        if not self.tm or not isinstance(getattr(node, "instr", None), OpInstr):
            return 0.0
        pressure = 0.0
        for edge in node.out_edges:
            if edge.edge_type == "setup":
                pressure = max(pressure, edge.weight)
            elif edge.edge_type == "data":
                dst_instr = getattr(edge.dst, "instr", None)
                if isinstance(dst_instr, OpInstr):
                    setup = self.tm.get_setup_time(
                        getattr(node.instr, "params", None),
                        getattr(dst_instr, "params", None),
                    )
                    pressure = max(pressure, float(setup or 0.0))
        return pressure

    def _structure_hazard_priority(self, node: MDGNode) -> float:
        """Prioritize lower setup costs when ready operations compete for scarce PALU instances."""
        if not self.tm or not hasattr(self, "_ready_set") or node not in self._ready_set:
            return 0.0

        compatible = self._get_compatible_palu_types(node)
        if not compatible:
            return 0.0


        competing = [
            n for n in self._ready_set if n != node
            and set(self._get_compatible_palu_types(n)) & set(compatible)
        ]
        if not competing:
            return 0.0  


        best_bonus = 0.0
        for pid in compatible:
            current_model = self._palu_last_model.get(pid)
            if not current_model:
                continue
            node_instr = node.instr
            if not isinstance(node_instr, OpInstr) or not getattr(node_instr, "params", None):
                continue
            setup_cost = float(
                self.tm.get_setup_time(current_model, node_instr.params) or 0.0
            )
            if setup_cost == 0.0:
                best_bonus = max(best_bonus, 50.0)
            else:
                best_bonus = max(best_bonus, 20.0 - setup_cost)

        return best_bonus


    def _copy_state(self, state: ResourceState) -> ResourceState:
        return ResourceState(
            palu_instances={pid: list(times) for pid, times in state.palu_instances.items()},
            material_status=dict(state.material_status),
            cache_occupancy={k: set(v) for k, v in state.cache_occupancy.items()},
            cache_capacity=dict(state.cache_capacity),
            device_last_model=dict(state.device_last_model),
        )

    def _earliest_feasible_time(self, node: MDGNode, state: ResourceState, current_time: float) -> float:
        """Return the earliest issue time from timing constraints; capacity is checked separately."""
        t = current_time

        compatible = self._get_compatible_palu_types(node)
        if compatible:
            earliest_palu_available = float('inf')
            for pid in compatible:
                instances = state.palu_instances.get(pid, [])
                for busy_until in instances:
                    if busy_until > t:
                        earliest_palu_available = min(earliest_palu_available, busy_until)
                    else:
                        earliest_palu_available = t
            if earliest_palu_available < float('inf'):
                t = max(t, earliest_palu_available)
        for mat in self._get_input_materials(node):
            status = state.material_status.get(mat)
            if status:
                t = max(t, status[2])
        for e in node.in_edges:
            if e.edge_type == "data" and e.src.block.id != node.block.id:
                # Only direct-predecessor dependencies apply across mutually exclusive paths.
                if e.src.block not in node.block.predecessors:
                    continue
                mat = None
                if isinstance(e.src.instr, OpInstr):
                    if e.src.instr.target:
                        mat = e.src.instr.target
                    elif e.src.instr.implicit_targets:
                        mat = e.src.instr.implicit_targets[0]
                if mat and mat in state.material_status:
                    t = max(t, state.material_status[mat][2])
                elif mat:
                    t = float('inf')
        return t

    def _cross_block_ready(self, node: MDGNode, state: ResourceState, current_time: float) -> bool:

        for e in node.in_edges:
            if e.edge_type == "data" and e.src.block.id != node.block.id:
                # Only direct-predecessor dependencies apply across mutually exclusive paths.
                if e.src.block not in node.block.predecessors:
                    continue
                mat = None
                if isinstance(e.src.instr, OpInstr):
                    if e.src.instr.target:
                        mat = e.src.instr.target
                    elif e.src.instr.implicit_targets:
                        mat = e.src.instr.implicit_targets[0]
                if mat and mat in state.material_status:
                    holder, _loc, ready_time = state.material_status[mat]
                    if ready_time > current_time:
                        return False
                elif mat:
                    return False
        return True

    def _resource_feasible(self, node: MDGNode, state: ResourceState, current_time: float) -> bool:

        compatible = self._get_compatible_palu_types(node)
        if compatible:
            has_free = False
            for pid in compatible:
                instances = state.palu_instances.get(pid, [])
                if any(t <= current_time + 1e-9 for t in instances):
                    has_free = True
                    break
            if not has_free:
                return False

        for mat in self._get_input_materials(node):
            status = state.material_status.get(mat)
            if status is not None:
                holder, _loc, ready_time = status
                if holder is not None and holder != node.id and ready_time > current_time:
                    return False
                if holder is None and ready_time > current_time:
                    return False

        _src, dst = self._get_storage_types(node)
        if dst and dst in state.cache_capacity:
            cap = state.cache_capacity[dst]
            occ = len(state.cache_occupancy.get(dst, set()))
            out_mats = self._get_output_materials(node)
            new_cnt = len([m for m in out_mats if m not in state.cache_occupancy.get(dst, set())])
            if occ + new_cnt > cap:
                return False

        return True

    def _allocate(self, node: MDGNode, state: ResourceState, start_time: float) -> float:
        latency = self._get_latency(node)
        finish = start_time + latency


        best_pid, best_idx, _ = self._find_best_palu_instance(node, state, start_time)
        if best_pid is not None and best_idx is not None:
            state.palu_instances[best_pid][best_idx] = finish
            self._node_palu_allocation[node.id] = (best_pid, best_idx)

            if isinstance(node.instr, OpInstr) and getattr(node.instr, "params", None):
                self._palu_last_model[best_pid] = node.instr.params


        dev = self._get_device(node)
        if dev:
            state.device_last_model[dev] = (getattr(node.instr, "params", None) or "")
            if isinstance(node.instr, OpInstr) and getattr(node.instr, "params", None):
                self._device_last_model[dev] = node.instr.params

        src_type, dst_type = self._get_storage_types(node)

        for mat in self._get_input_materials(node):
            status = state.material_status.get(mat)
            if status:
                _holder, loc, _rt = status
                if loc and loc in state.cache_occupancy:
                    state.cache_occupancy[loc].discard(mat)
            state.material_status[mat] = (node.id, None, finish)

        for mat in self._get_output_materials(node):
            state.material_status[mat] = (node.id, dst_type, finish)

        return finish

    def _release(self, node: MDGNode, state: ResourceState):

        alloc = self._node_palu_allocation.pop(node.id, None)
        if alloc is not None:
            palu_id, inst_idx = alloc
            if palu_id in state.palu_instances and inst_idx < len(state.palu_instances[palu_id]):
                state.palu_instances[palu_id][inst_idx] = 0.0

        _src, dst = self._get_storage_types(node)
        for mat in self._get_output_materials(node):
            status = state.material_status.get(mat)
            if status and status[0] == node.id:
                state.material_status[mat] = (None, dst, status[2])
                if dst:
                    state.cache_occupancy.setdefault(dst, set()).add(mat)

    def override_initial_state(self, runtime_state_dict: Dict) -> ResourceState:
        """Initialize resource occupancy, availability, locations, and setup history from a runtime snapshot."""
        state = ResourceState()
        state.cache_capacity = dict(self.cache_capacity)


        state.palu_instances = {pid: list(times) for pid, times in self.initial_palu_instances.items()}


        palu_busy = runtime_state_dict.get("palu_busy_until", {})
        for pid, busy_list in palu_busy.items():
            if pid in state.palu_instances:
                instances = state.palu_instances[pid]
                for i, busy_until in enumerate(busy_list):
                    if i < len(instances):
                        instances[i] = float(busy_until)

        for mat, (loc, ready_time) in runtime_state_dict.get("material_locations", {}).items():
            state.material_status[mat] = (None, loc, float(ready_time))

        for storage_type, mats in runtime_state_dict.get("buffer_occupancy", {}).items():
            state.cache_occupancy[storage_type] = set(mats)

        for stype, cap in runtime_state_dict.get("cache_capacity", {}).items():
            state.cache_capacity[stype] = int(cap)


        for pid, params in runtime_state_dict.get("palu_last_model", {}).items():
            self._palu_last_model[int(pid)] = str(params)


        for dev, params in runtime_state_dict.get("device_last_model", {}).items():
            self._device_last_model[dev] = str(params)

        return state


    def schedule_block(
        self, block: BasicBlock, initial_state: ResourceState
    ) -> Tuple[List[Tuple[MDGNode, float, float]], ResourceState]:
        entry_node = self.mdg.block_entry.get(block.id)
        if entry_node is None:
            return [], initial_state

        block_nodes = [n for n in self.mdg.nodes if n.block.id == block.id and n.node_type == "op"]
        if not block_nodes:
            return [], initial_state

        state = self._copy_state(initial_state)
        if not state.cache_capacity:
            state.cache_capacity = dict(self.cache_capacity)
        if not state.palu_instances:
            state.palu_instances = {pid: list(times) for pid, times in self.initial_palu_instances.items()}

        block_node_set = set(block_nodes)
        in_deg = {}
        for node in block_nodes:
            deg = sum(
                1
                for e in node.in_edges
                if e.edge_type in ("data", "struct")
                and e.src in block_node_set
            )
            in_deg[node.id] = deg

        ready = {n for n in block_nodes if in_deg[n.id] == 0}
        self._ready_set = ready  
        active: Dict[int, float] = {}
        scheduled_ids: Set[int] = set()
        schedule: List[Tuple[MDGNode, float, float]] = []
        current_time = 0.0


        device_timeline: Dict[str, List[Tuple[float, float, str]]] = {}
        material_timeline: Dict[str, List[Tuple[float, float, str]]] = {}

        while ready or active:
            fired_any = False
            while True:
                candidates = [
                    n for n in ready
                    if self._cross_block_ready(n, state, current_time)
                    and self._resource_feasible(n, state, current_time)
                ]
                if not candidates:
                    break
                candidates.sort(key=lambda n: self._priority(n), reverse=True)
                fired_this_round = False
                for node in candidates:
                    if self._cross_block_ready(node, state, current_time) and self._resource_feasible(node, state, current_time):
                        finish = self._allocate(node, state, current_time)
                        active[node.id] = finish
                        ready.remove(node)
                        schedule.append((node, current_time, finish))

                        if isinstance(node.instr, OpInstr):
                            node.instr.scheduled_start = current_time
                            node.instr.scheduled_finish = finish

                        dev = self._get_device(node)
                        if dev:
                            device_timeline.setdefault(dev, []).append(
                                (current_time, finish, str(node.instr))
                            )
                        for mat in self._get_input_materials(node):
                            material_timeline.setdefault(mat, []).append(
                                (current_time, finish, str(node.instr))
                            )

                        fired_this_round = True
                        fired_any = True
                if not fired_this_round:
                    break

            if not active and not ready:
                break

            if not active:
                # Advance to the next release when ready operations are blocked by time constraints.
                earliest = min(self._earliest_feasible_time(n, state, current_time) for n in ready)
                if earliest > current_time:
                    current_time = earliest
                    continue
                break


            next_finish = min(active.values())
            if self.tick > 0:
                next_tick = current_time + self.tick
                next_time = min(next_finish, next_tick)
            else:
                next_time = next_finish


            finished = [nid for nid, ft in list(active.items()) if ft <= next_time]
            for nid in finished:
                del active[nid]
                scheduled_ids.add(nid)
                n = self.node_map[nid]
                self._release(n, state)
                for e in n.out_edges:
                    if e.edge_type in ("data", "struct") and e.dst in block_node_set:
                        in_deg[e.dst.id] -= 1
                        if in_deg[e.dst.id] == 0:
                            ready.add(e.dst)

            self._ready_set = ready  
            current_time = next_time

        return schedule, state
