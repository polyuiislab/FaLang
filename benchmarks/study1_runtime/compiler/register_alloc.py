"""Allocate material versions to workstation registers using capability, lifetime, and transfer constraints in PBQP."""
from typing import Dict, List, Tuple, Optional, Set
import math

from compiler.ir import OpInstr, PhiInstr, BasicBlock, ControlFlowGraph
from compiler.mdg import ManufacturingDependencyGraph, MDGNode
from compiler.scheduler import ScheduleResult
from compiler.register_alloc_pbqp import PBQPGraph, PBQPVector, PBQPMatrix, PBQPSolver
from core.target_model import TargetModelLoader


class RegisterAllocator:


    def __init__(
        self,
        cfg: ControlFlowGraph,
        mdg: ManufacturingDependencyGraph,
        schedule_result: ScheduleResult,
        target_model: TargetModelLoader,
    ):
        self.cfg = cfg
        self.mdg = mdg
        self.schedule_result = schedule_result
        self.tm = target_model

        self.vregs: List[str] = []
        self.pregs: List = []  
        self.allocation: Dict[str, any] = {}  


        self._preg_to_ws: Dict[int, int] = {}
        self._ws_to_pregs: Dict[int, List[int]] = {}
        self._ws_capabilities: Dict[int, Set[Tuple[str, str, str]]] = {}
        self._cap_to_ws: Dict[Tuple[str, str, str], List[int]] = {}


        self.def_instrs: Dict[str, Tuple[int, int, any]] = {}
        self.use_instrs: Dict[str, List[Tuple[int, int, any]]] = {}
        self.vreg_liveness: Dict[str, Tuple[float, float]] = {}
        self.normal_liveness: Dict[str, Tuple[float, float]] = {}
        self.exception_liveness: Dict[str, Tuple[float, float]] = {}

        self._build_mappings()
        self._collect_pregs()
        self._collect_vregs()
        self._analyze_uses()
        self._compute_liveness()
        self.normal_liveness = dict(self.vreg_liveness)
        self.exception_liveness = self._compute_exception_liveness()
        self._build_instr_time_map()


    def _build_mappings(self):

        addr = self.tm.data.get("addressing_model", {}) if self.tm and self.tm.data else {}
        ws_map = addr.get("workstation_mapping", [])

        for ws in ws_map:
            ws_id = ws.get("workstation_id")
            for reg_id in ws.get("assigned_registers", []):
                self._preg_to_ws[reg_id] = ws_id
                self._ws_to_pregs.setdefault(ws_id, []).append(reg_id)

        ics = self.tm.data.get("instruction_capability_set", {}) if self.tm and self.tm.data else {}
        palu_defs = {}
        for p in ics.get("palu_definitions", []):
            palu_defs[p.get("palu_id")] = p

        for ws in ws_map:
            ws_id = ws.get("workstation_id")
            caps = set()
            for palu_id in ws.get("assigned_palus", []):
                palu = palu_defs.get(palu_id)
                if not palu:
                    continue
                for c in palu.get("supported_capabilities", []):
                    cap = (str(c.get("opcode", "")), str(c.get("func", "")), str(c.get("rp", "")))
                    if cap[0] and cap[1] and cap[2]:
                        caps.add(cap)
                        self._cap_to_ws.setdefault(cap, []).append(ws_id)
            self._ws_capabilities[ws_id] = caps

    def _collect_pregs(self):

        regions = self.tm.get_storage_regions() if self.tm else {}
        reg_ids = list(regions.get("register_ids", []))
        self.pregs = reg_ids + ["memory"]


    def _is_vreg(self, name: str) -> bool:
        if not isinstance(name, str) or not name:
            return False
        if name.isdigit() or name.startswith('"'):
            return False
        res_names = getattr(self.cfg, "resource_names", set()) or set()
        if name in res_names:
            return False
        parts = name.rsplit("_", 1)
        return len(parts) == 2 and parts[1].isdigit()

    def _material_arg_indices(self, op_type: str, argc: int):

        if op_type == "Process":
            return range(1, argc)
        if op_type == "Inspect":
            return [1] if argc >= 2 else []
        if op_type in ("Move", "Load", "Store"):
            return [0] if argc >= 1 else []
        if op_type == "Assign":
            return [0] if argc >= 1 else []
        return []

    def _is_exception_path_block(self, block: BasicBlock) -> bool:
        """Identify catch and compensate blocks, which have separate exception-path allocation."""
        name = getattr(block, "name", "") or ""
        return name.startswith("Catch_") or name.startswith("Compensate")

    def _collect_vregs(self):

        vregs = set()
        for block in self.cfg.blocks:
            if self._is_exception_path_block(block):
                continue
            for instr in block.instructions:
                if isinstance(instr, OpInstr):
                    if instr.target and self._is_vreg(instr.target):
                        vregs.add(instr.target)
                    for it in instr.implicit_targets or []:
                        if self._is_vreg(it):
                            vregs.add(it)
                    for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                        if i < len(instr.args) and self._is_vreg(instr.args[i]):
                            vregs.add(instr.args[i])
                elif isinstance(instr, PhiInstr):
                    if self._is_vreg(instr.target):
                        vregs.add(instr.target)
                    for v in instr.choices.values():
                        if self._is_vreg(v):
                            vregs.add(v)
        self.vregs = sorted(vregs)


    def _analyze_uses(self):
        for block in self.cfg.blocks:
            if self._is_exception_path_block(block):
                continue
            for idx, instr in enumerate(block.instructions):
                if isinstance(instr, OpInstr):
                    defs = []
                    if instr.target and self._is_vreg(instr.target):
                        defs.append(instr.target)
                    for it in instr.implicit_targets or []:
                        if self._is_vreg(it):
                            defs.append(it)
                    for d in defs:
                        self.def_instrs[d] = (block.id, idx, instr)

                    for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                        if i < len(instr.args) and self._is_vreg(instr.args[i]):
                            self.use_instrs.setdefault(instr.args[i], []).append((block.id, idx, instr))

                elif isinstance(instr, PhiInstr):
                    if self._is_vreg(instr.target):
                        self.def_instrs[instr.target] = (block.id, idx, instr)
                    for v in instr.choices.values():
                        if self._is_vreg(v):
                            self.use_instrs.setdefault(v, []).append((block.id, idx, instr))


    def _compute_liveness(self):
        """Derive holding lifetimes from the pre-allocation schedule or MDG timing."""
        if (self.schedule_result is None
                or not getattr(self.schedule_result, "block_schedules", None)):

            self._compute_liveness_from_mdg()
            return

        node_times = {}
        for block_id, sched in self.schedule_result.block_schedules.items():
            for node, st, fin in sched:
                node_times[node.id] = (st, fin)

        mdg_node_map = {}
        for node in self.mdg.nodes:
            if node.instr and hasattr(node.instr, "op_type"):
                if node.block and self._is_exception_path_block(node.block):
                    continue
                for block in self.cfg.blocks:
                    if self._is_exception_path_block(block):
                        continue
                    if block.id == node.block.id:
                        for idx, instr in enumerate(block.instructions):
                            if instr is node.instr:
                                mdg_node_map[(block.id, idx)] = node
                                break

        for vreg in self.vregs:
            start = float("inf")
            end = 0.0

            if vreg in self.def_instrs:
                bid, idx, _ = self.def_instrs[vreg]
                node = mdg_node_map.get((bid, idx))
                if node and node.id in node_times:
                    st, fin = node_times[node.id]
                    start = min(start, st)
                    end = max(end, fin)
                else:
                    start = 0.0
                    end = float("inf")

            for bid, idx, _ in self.use_instrs.get(vreg, []):
                node = mdg_node_map.get((bid, idx))
                if node and node.id in node_times:
                    st, fin = node_times[node.id]
                    start = min(start, st)
                    end = max(end, fin)
                else:
                    end = float("inf")

            if start == float("inf"):
                start = 0.0
            self.vreg_liveness[vreg] = (start, end)

    def _compute_liveness_from_mdg(self):
        """Estimate lifetimes by ASAP propagation over MDG data and structural edges."""
        from collections import deque


        op_nodes = [n for n in self.mdg.nodes if n.node_type == "op"]
        if not op_nodes:
            return

        nids = {n.id for n in op_nodes}


        in_deg: Dict[int, int] = {n.id: 0 for n in op_nodes}
        adj: Dict[int, List[MDGEdge]] = {n.id: [] for n in op_nodes}
        for n in op_nodes:
            for e in n.in_edges:
                if e.edge_type in ("data", "struct") and e.src.id in nids:
                    in_deg[n.id] += 1
            for e in n.out_edges:
                if e.edge_type in ("data", "struct") and e.dst.id in nids:
                    adj.setdefault(n.id, []).append(e)


        queue = deque(nid for nid, deg in in_deg.items() if deg == 0)
        order: List[int] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for e in adj.get(nid, []):
                in_deg[e.dst.id] -= 1
                if in_deg[e.dst.id] == 0:
                    queue.append(e.dst.id)

        if not order:

            return


        asap: Dict[int, float] = {}
        for nid in order:
            t = 0.0
            node = self.mdg.nodes[nid] if nid < len(self.mdg.nodes) else None
            if node is None:
                continue
            for e in node.in_edges:
                if e.edge_type in ("data", "struct") and e.src.id in asap:
                    t = max(t, asap[e.src.id] + e.weight)
            asap[nid] = t


        def _node_latency(node: MDGNode) -> float:
            if not isinstance(node.instr, OpInstr):
                return 0.0
            if node.instr.expected_time is not None:
                return float(node.instr.expected_time)
            return 0.0


        makespan = 0.0
        for nid, start_t in asap.items():
            node = next((n for n in op_nodes if n.id == nid), None)
            if node:
                makespan = max(makespan, start_t + _node_latency(node))


        vreg_def_node: Dict[str, MDGNode] = {}
        vreg_use_nodes: Dict[str, List[MDGNode]] = {}

        for node in op_nodes:
            if not isinstance(node.instr, OpInstr):
                continue

            if node.instr.target and self._is_vreg(node.instr.target):
                vreg_def_node[node.instr.target] = node
            for it in (node.instr.implicit_targets or []):
                if self._is_vreg(it):
                    vreg_def_node[it] = node

            for i in self._material_arg_indices(node.instr.op_type, len(node.instr.args)):
                if i < len(node.instr.args) and self._is_vreg(node.instr.args[i]):
                    vreg_use_nodes.setdefault(node.instr.args[i], []).append(node)


        for vreg in self.vregs:
            def_node = vreg_def_node.get(vreg)
            use_list = vreg_use_nodes.get(vreg, [])

            if def_node is not None and def_node.id in asap:
                start_time = asap[def_node.id]
            else:
                start_time = 0.0

            if use_list:
                end_time = max(
                    (asap.get(u.id, 0.0) + _node_latency(u)) for u in use_list
                )
            elif def_node is not None and def_node.id in asap:
                end_time = asap[def_node.id] + _node_latency(def_node)
            else:
                end_time = makespan

            self.vreg_liveness[vreg] = (start_time, end_time)


        self.instr_times = {}
        for node in op_nodes:
            if node.id in asap and node.instr is not None:
                latency = _node_latency(node)
                self.instr_times[id(node.instr)] = (asap[node.id], asap[node.id] + latency)

    def _compute_exception_liveness(self) -> Dict[str, Tuple[float, float]]:
        """Record exception-path material versions separately from the normal-path PBQP problem."""
        exception_vregs: Set[str] = set()
        def_sites: Dict[str, Tuple[int, int, OpInstr]] = {}
        use_sites: Dict[str, List[Tuple[int, int, OpInstr]]] = {}

        for block in self.cfg.blocks:
            if not self._is_exception_path_block(block):
                continue
            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue
                if instr.target and self._is_vreg(instr.target):
                    exception_vregs.add(instr.target)
                    def_sites[instr.target] = (block.id, idx, instr)
                for it in instr.implicit_targets or []:
                    if self._is_vreg(it):
                        exception_vregs.add(it)
                        def_sites[it] = (block.id, idx, instr)
                for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                    if i < len(instr.args) and self._is_vreg(instr.args[i]):
                        vreg = instr.args[i]
                        exception_vregs.add(vreg)
                        use_sites.setdefault(vreg, []).append((block.id, idx, instr))

        node_times = {}
        for sched in self.schedule_result.block_schedules.values():
            for node, st, fin in sched:
                node_times[id(node.instr)] = (st, fin)

        liveness: Dict[str, Tuple[float, float]] = {}
        for vreg in sorted(exception_vregs):
            start = float("inf")
            end = 0.0
            if vreg in def_sites:
                _, _, instr = def_sites[vreg]
                st, fin = node_times.get(id(instr), (0.0, float("inf")))
                start = min(start, st)
                end = max(end, fin)
            for _, _, instr in use_sites.get(vreg, []):
                st, fin = node_times.get(id(instr), (0.0, float("inf")))
                start = min(start, st)
                end = max(end, fin)
            if start == float("inf"):
                start = 0.0
            liveness[vreg] = (start, end)
        return liveness


    def _get_instr_capability(self, instr: OpInstr) -> Optional[Tuple[str, str, str]]:
        if not isinstance(instr, OpInstr):
            return None
        key = self.tm.optype_to_instruction(instr.op_type)
        if key is None:
            return None
        opcode, func = key.opcode, key.func
        rp_code = instr.rp
        if not rp_code:
            rp_code = self.tm.resolve_rp_code(opcode, func, getattr(instr, "operator", None))
        if not rp_code:
            return None
        return (opcode, func, rp_code)

    def _is_logistics_cap(self, cap: Tuple[str, str, str]) -> bool:

        opcode, func, operator = cap
        return func in ("010", "000") and opcode in ("000001", "100101", "101101")

    def _get_preg_capacity(self, preg) -> int:
        if preg == "memory":
            return 10000
        storage_model = self.tm.data.get("storage_model", {}) if self.tm and self.tm.data else {}
        for entry in storage_model.get("capacity_constraints", []):
            if entry.get("storage_id") == preg:
                return entry.get("capacity", 1)
        return 1

    def _intervals_overlap(self, s1, e1, s2, e2) -> bool:
        return s1 < e2 and s2 < e1


    def _get_instr_exec_ws_and_preg(self, instr: OpInstr) -> Tuple[Optional[int], Optional[int]]:
        """Return the execution workstation and register, preferring explicit or implicit material outputs."""
        if not isinstance(instr, OpInstr):
            return None, None
        dominant = None
        if instr.target and self._is_vreg(instr.target):
            dominant = instr.target
        else:
            for extra in instr.implicit_targets or []:
                if isinstance(extra, str) and self._is_vreg(extra) and extra in self.allocation:
                    dominant = extra
                    break
        if dominant is None:
            for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                if i < len(instr.args) and self._is_vreg(instr.args[i]):
                    dominant = instr.args[i]
                    break
        if dominant and dominant in self.allocation:
            preg = self.allocation[dominant]
            if preg == "memory":

                for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                    if i < len(instr.args) and self._is_vreg(instr.args[i]):
                        alt = self.allocation.get(instr.args[i])
                        if alt != "memory":
                            ws = self._preg_to_ws.get(alt)
                            return ws, alt
                return None, None
            ws = self._preg_to_ws.get(preg)
            return ws, preg
        return None, None

    def _select_spill_register(self, ref_preg, ref_ws) -> Optional[int]:
        """Choose a nearby spill register, preferring capacity greater than two."""
        candidates = self._ws_to_pregs.get(ref_ws, [])
        if not candidates:
            return None

        large_cap = [c for c in candidates if self._get_preg_capacity(c) > 2]
        pool = large_cap if large_cap else candidates

        def distance(cand):
            if cand == ref_preg:
                return 0.0

            if ref_preg == "memory":
                return float('inf')
            lt = self.tm.get_logistics_time(ref_preg, cand) if self.tm else None
            return lt if lt is not None else float('inf')

        pool.sort(key=distance)
        return pool[0] if pool else None


    # Cache material-pair dependency facts once; they are independent of candidate-register pairs.


    def _precompute_pair_facts(self) -> None:
        self._data_dep_pairs: Set[Tuple[str, str]] = set()
        self._setup_cost_pairs: Dict[Tuple[str, str], float] = {}
        self._setup_edge_pairs: Dict[Tuple[str, str], float] = {}

        if self.mdg is None:
            return

        def defs_of(node) -> Set[str]:
            instr = node.instr
            if not isinstance(instr, OpInstr):
                return set()
            names = set()
            if instr.target:
                names.add(instr.target)
            names.update(instr.implicit_targets or [])
            return names

        def uses_of(node) -> Set[str]:
            instr = node.instr
            if not isinstance(instr, OpInstr):
                return set()
            names = set()
            for idx in self._material_arg_indices(instr.op_type, len(instr.args)):
                if idx < len(instr.args):
                    names.add(instr.args[idx])
            return names

        for edge in self.mdg.edges:
            src_defs = defs_of(edge.src)
            dst_defs = defs_of(edge.dst)
            if not src_defs and not dst_defs:
                continue
            if edge.edge_type == "data":
                for a in src_defs:
                    for b in uses_of(edge.dst):
                        self._data_dep_pairs.add((a, b))
                setup = 0.0
                if self.tm:
                    setup = float(self.tm.get_setup_time(
                        getattr(edge.src.instr, "params", None),
                        getattr(edge.dst.instr, "params", None),
                    ) or 0.0)
                dst_uses = uses_of(edge.dst)
                for a in src_defs:
                    if a not in dst_uses:
                        continue
                    for b in dst_defs:
                        key = (a, b)
                        self._setup_cost_pairs[key] = max(self._setup_cost_pairs.get(key, 0.0), setup)
                for b in src_defs:
                    if b not in dst_uses:
                        continue
                    for a in dst_defs:
                        key = (b, a)
                        self._setup_cost_pairs[key] = max(self._setup_cost_pairs.get(key, 0.0), setup)
            elif edge.edge_type == "setup":
                for a in src_defs:
                    for b in dst_defs:
                        for key in ((a, b), (b, a)):
                            self._setup_edge_pairs[key] = max(
                                self._setup_edge_pairs.get(key, 0.0), float(edge.weight)
                            )

    def _has_mdg_data_dependency(self, vreg_a: str, vreg_b: str) -> bool:

        pairs = getattr(self, "_data_dep_pairs", None)
        if pairs is not None:
            return (vreg_a, vreg_b) in pairs
        return self._has_mdg_data_dependency_scan(vreg_a, vreg_b)

    def _has_mdg_data_dependency_scan(self, vreg_a: str, vreg_b: str) -> bool:

        for node in self.mdg.nodes:
            if not isinstance(node.instr, OpInstr):
                continue
            prod_a = False
            if node.instr.target == vreg_a:
                prod_a = True
            if vreg_a in (node.instr.implicit_targets or []):
                prod_a = True
            if not prod_a:
                continue
            for e in node.out_edges:
                if e.edge_type != "data":
                    continue
                dst = e.dst
                if not isinstance(dst.instr, OpInstr):
                    continue
                idxs = self._material_arg_indices(dst.instr.op_type, len(dst.instr.args))
                for i in idxs:
                    if i < len(dst.instr.args) and dst.instr.args[i] == vreg_b:
                        return True
        return False

    def _has_mdg_setup_dependency(self, vreg_a: str, vreg_b: str) -> float:
        """Return the largest setup-edge cost between two material versions."""
        pairs = getattr(self, "_setup_edge_pairs", None)
        if pairs is not None:
            return pairs.get((vreg_a, vreg_b), 0.0)
        return self._has_mdg_setup_dependency_scan(vreg_a, vreg_b)

    def _has_mdg_setup_dependency_scan(self, vreg_a: str, vreg_b: str) -> float:

        best_cost = 0.0
        for node in self.mdg.nodes:
            if not isinstance(node.instr, OpInstr):
                continue
            is_a = (node.instr.target == vreg_a or vreg_a in (node.instr.implicit_targets or []))
            is_b = (node.instr.target == vreg_b or vreg_b in (node.instr.implicit_targets or []))
            if not is_a and not is_b:
                continue
            for e in node.out_edges:
                if e.edge_type != "setup":
                    continue
                dst = e.dst
                if not isinstance(dst.instr, OpInstr):
                    continue
                dst_is_a = (dst.instr.target == vreg_a or vreg_a in (dst.instr.implicit_targets or []))
                dst_is_b = (dst.instr.target == vreg_b or vreg_b in (dst.instr.implicit_targets or []))
                if (is_a and dst_is_b) or (is_b and dst_is_a):
                    best_cost = max(best_cost, e.weight)
        return best_cost

    def _setup_cost_for_dependency(self, vreg_a: str, vreg_b: str) -> float:

        pairs = getattr(self, "_setup_cost_pairs", None)
        if pairs is not None:
            return pairs.get((vreg_a, vreg_b), 0.0)
        return self._setup_cost_for_dependency_scan(vreg_a, vreg_b)

    def _setup_cost_for_dependency_scan(self, vreg_a: str, vreg_b: str) -> float:
        if not self.tm:
            return 0.0
        best = 0.0
        for edge in self.mdg.edges:
            if edge.edge_type != "data":
                continue
            src = edge.src
            dst = edge.dst
            if not isinstance(src.instr, OpInstr) or not isinstance(dst.instr, OpInstr):
                continue

            src_defs = set()
            if src.instr.target:
                src_defs.add(src.instr.target)
            src_defs.update(src.instr.implicit_targets or [])

            dst_defs = set()
            if dst.instr.target:
                dst_defs.add(dst.instr.target)
            dst_defs.update(dst.instr.implicit_targets or [])

            dst_uses = set()
            for idx in self._material_arg_indices(dst.instr.op_type, len(dst.instr.args)):
                if idx < len(dst.instr.args):
                    dst_uses.add(dst.instr.args[idx])

            forward = vreg_a in src_defs and vreg_a in dst_uses and vreg_b in dst_defs
            reverse = vreg_b in src_defs and vreg_b in dst_uses and vreg_a in dst_defs
            if forward or reverse:
                setup = self.tm.get_setup_time(
                    getattr(src.instr, "params", None),
                    getattr(dst.instr, "params", None),
                )
                best = max(best, float(setup or 0.0))
        return best

    def _build_instr_time_map(self):

        self.instr_times: Dict[int, Tuple[float, float]] = {}
        for block_id, sched in self.schedule_result.block_schedules.items():
            for node, st, fin in sched:
                if node.instr is not None:
                    self.instr_times[id(node.instr)] = (st, fin)

    def _same_palu_competition(self, vi: str, vj: str) -> bool:
        """Detect overlapping operations that compete for the same PALU type at one workstation."""
        if vi not in self.def_instrs or vj not in self.def_instrs:
            return False
        _, _, instr_i = self.def_instrs[vi]
        _, _, instr_j = self.def_instrs[vj]
        if not isinstance(instr_i, OpInstr) or not isinstance(instr_j, OpInstr):
            return False


        key_i = self.tm.optype_to_instruction(instr_i.op_type)
        key_j = self.tm.optype_to_instruction(instr_j.op_type)
        if key_i is None or key_j is None:
            return False
        if key_i.opcode != key_j.opcode or key_i.func != key_j.func:
            return False


        t_i = self.instr_times.get(id(instr_i))
        t_j = self.instr_times.get(id(instr_j))
        if t_i is None or t_j is None:

            return True
        si, ei = t_i
        sj, ej = t_j
        return self._intervals_overlap(si, ei, sj, ej)

    def _same_instr_usage(self, vreg_a: str, vreg_b: str) -> bool:


        if vreg_a in self.def_instrs and vreg_b in self.def_instrs:
            ba, ia, _ = self.def_instrs[vreg_a]
            bb, ib, _ = self.def_instrs[vreg_b]
            if ba == bb and ia == ib:
                return True

        for ba, ia, _ in self.use_instrs.get(vreg_a, []):
            for bb, ib, _ in self.use_instrs.get(vreg_b, []):
                if ba == bb and ia == ib:
                    return True

        if vreg_a in self.def_instrs:
            ba, ia, _ = self.def_instrs[vreg_a]
            for bb, ib, _ in self.use_instrs.get(vreg_b, []):
                if ba == bb and ia == ib:
                    return True
        if vreg_b in self.def_instrs:
            ba, ia, _ = self.def_instrs[vreg_b]
            for bb, ib, _ in self.use_instrs.get(vreg_a, []):
                if ba == bb and ia == ib:
                    return True
        return False


    def _build_pbqp(self) -> PBQPGraph:
        graph = PBQPGraph()
        vreg_to_id = {v: i for i, v in enumerate(self.vregs)}
        n_pregs = len(self.pregs)
        self._precompute_pair_facts()


        ws_of = {preg: self._preg_to_ws.get(preg) for preg in self.pregs}
        cap_of = {preg: (None if preg == "memory" else self._get_preg_capacity(preg))
                  for preg in self.pregs}
        logistics_of: Dict[Tuple[int, int], Optional[float]] = {}
        for pi in self.pregs:
            for pj in self.pregs:
                if pi == "memory" or pj == "memory":
                    continue
                if ws_of.get(pi) is not None and ws_of.get(pj) is not None:
                    logistics_of[(pi, pj)] = self.tm.get_logistics_time(pi, pj) if self.tm else None


        for vreg in self.vregs:
            costs = []
            def_cap = None
            if vreg in self.def_instrs:
                _, _, instr = self.def_instrs[vreg]
                def_cap = self._get_instr_capability(instr)
            use_caps = set()
            for _, _, instr in self.use_instrs.get(vreg, []):
                cap = self._get_instr_capability(instr)
                if cap:
                    use_caps.add(cap)

            for preg in self.pregs:
                cost = 0.0
                if preg == "memory":
                    cost += 500.0
                else:
                    cost += 1.0
                    ws_id = self._preg_to_ws.get(preg)
                    if ws_id is not None:
                        ws_caps = self._ws_capabilities.get(ws_id, set())
                        # The defining operation requires a capable workstation.
                        if def_cap and def_cap not in ws_caps:
                            if not self._is_logistics_cap(def_cap):
                                cost = float("inf")
                        # Reward sites that also support consuming operations, reducing transfers.
                        if cost != float("inf") and use_caps and use_caps.issubset(ws_caps):
                            cost -= 3.0
                    else:
                        cost = float("inf")
                costs.append(cost)
            graph.add_node(vreg_to_id[vreg], PBQPVector(costs))


        for i, vi in enumerate(self.vregs):
            si, ei = self.vreg_liveness.get(vi, (0, float("inf")))
            for j in range(i + 1, len(self.vregs)):
                vj = self.vregs[j]
                sj, ej = self.vreg_liveness.get(vj, (0, float("inf")))
                overlap = self._intervals_overlap(si, ei, sj, ej)
                palu_competition = self._same_palu_competition(vi, vj)
                has_data_dep = (self._has_mdg_data_dependency(vi, vj)
                                or self._has_mdg_data_dependency(vj, vi))
                dependency_setup = self._setup_cost_for_dependency(vi, vj) if has_data_dep else 0.0
                setup_dep = self._has_mdg_setup_dependency(vi, vj)
                same_instr = self._same_instr_usage(vi, vj)

                matrix = [[0.0] * n_pregs for _ in range(n_pregs)]
                has_nonzero = False

                for pi_idx, pi in enumerate(self.pregs):
                    pi_ws = ws_of[pi]
                    for pj_idx, pj in enumerate(self.pregs):
                        cost = 0.0
                        pj_ws = ws_of[pj]
                        both_ws_known = pi_ws is not None and pj_ws is not None


                        if pi == pj and pi != "memory":
                            if overlap:
                                cap = cap_of[pi]
                                if cap <= 1:
                                    cost = float("inf")
                                else:
                                    cost += max(0.5, 4.0 / cap)
                                has_nonzero = True
                            if palu_competition:
                                cost += 25.0
                                has_nonzero = True


                        if has_data_dep and both_ws_known:
                            if pi_ws != pj_ws:
                                lt = logistics_of.get((pi, pj))
                                cost += lt if lt is not None else 10.0
                            else:
                                cost -= 2.0
                            has_nonzero = True
                            if dependency_setup:
                                cost += dependency_setup
                                has_nonzero = True


                        if setup_dep > 0 and both_ws_known and pi_ws == pj_ws:
                            cost += setup_dep
                            has_nonzero = True


                        if same_instr and both_ws_known:
                            cost += 50.0 if pi_ws != pj_ws else -5.0
                            has_nonzero = True

                        matrix[pi_idx][pj_idx] = cost

                if has_nonzero:
                    graph.add_edge(vreg_to_id[vi], vreg_to_id[vj], PBQPMatrix(matrix))

        return graph

    def _build_coloring_interference(self) -> Dict[str, Set[str]]:

        graph: Dict[str, Set[str]] = {v: set() for v in self.vregs}
        for i, vi in enumerate(self.vregs):
            si, ei = self.vreg_liveness.get(vi, (0.0, float("inf")))
            for vj in self.vregs[i + 1:]:
                sj, ej = self.vreg_liveness.get(vj, (0.0, float("inf")))
                overlaps = self._intervals_overlap(si, ei, sj, ej)
                linear_hazard = self._same_instr_usage(vi, vj)
                data_related = (
                    self._has_mdg_data_dependency(vi, vj)
                    or self._has_mdg_data_dependency(vj, vi)
                )
                if overlaps or linear_hazard or (data_related and overlaps):
                    graph[vi].add(vj)
                    graph[vj].add(vi)
        return graph

    def _candidate_colors_for_vreg(self, vreg: str) -> List[int]:

        candidates: List[int] = []
        for preg in self.pregs:
            if preg == "memory":
                continue
            if self._preg_supports_vreg_capabilities(preg, vreg):
                candidates.append(preg)
        return candidates

    def _color_capacity_ok(self, vreg: str, preg: int, interference: Dict[str, Set[str]]) -> bool:

        cap = self._get_preg_capacity(preg)
        if cap <= 1:
            return all(self.allocation.get(n) != preg for n in interference.get(vreg, set()))

        start, end = self.vreg_liveness.get(vreg, (0.0, float("inf")))
        active = 1
        for other, other_preg in self.allocation.items():
            if other_preg != preg:
                continue
            os, oe = self.vreg_liveness.get(other, (0.0, float("inf")))
            if self._intervals_overlap(start, end, os, oe):
                active += 1
        return active <= cap

    def _color_score(self, vreg: str, preg: int, interference: Dict[str, Set[str]]) -> float:
        """Score feasible colors by transfer, setup, and available capacity."""
        score = 0.0
        ws = self._preg_to_ws.get(preg)
        cap = max(1, self._get_preg_capacity(preg))
        used = sum(1 for assigned in self.allocation.values() if assigned == preg)
        score += used / cap

        for other in interference.get(vreg, set()):
            other_preg = self.allocation.get(other)
            if other_preg is None or other_preg == "memory":
                continue
            other_ws = self._preg_to_ws.get(other_preg)
            if ws is None or other_ws is None:
                continue
            if self._has_mdg_data_dependency(vreg, other) or self._has_mdg_data_dependency(other, vreg):
                if ws == other_ws:
                    score -= 2.0
                else:
                    lt = self.tm.get_logistics_time(preg, other_preg) if self.tm else None
                    score += lt if lt is not None else 10.0
            setup = self._has_mdg_setup_dependency(vreg, other)
            if setup and ws == other_ws:
                score += setup
        return score

    def allocate_graph_coloring(self) -> Dict[str, any]:
        """Allocate by graph coloring with capability filtering and capacity-aware color selection."""
        if not self.vregs:
            return {}

        interference = self._build_coloring_interference()

        def order_key(vreg: str):
            start, end = self.vreg_liveness.get(vreg, (0.0, float("inf")))
            lifetime = end - start if math.isfinite(end) else float("inf")
            return (-len(interference.get(vreg, set())), -lifetime, start)

        for vreg in sorted(self.vregs, key=order_key):
            candidates = [
                preg for preg in self._candidate_colors_for_vreg(vreg)
                if self._color_capacity_ok(vreg, preg, interference)
            ]
            if not candidates:
                self.allocation[vreg] = "memory"
                continue
            self.allocation[vreg] = min(
                candidates,
                key=lambda preg: self._color_score(vreg, preg, interference),
            )

        self._fix_capacity_violations()
        self._insert_cross_ws_moves()
        self._insert_spill_load_store()
        self._assign_palus()
        return self.allocation

    def allocate_fast(self) -> Dict[str, any]:
        """Use capability-matched registers without PBQP or transfer/spill insertion."""
        if not self.vregs:
            return {}


        ordered_vregs = sorted(
            self.vregs,
            key=lambda v: self.vreg_liveness.get(v, (0, float("inf")))[0]
        )


        ws_reg_usage: Dict[int, Dict[int, int]] = {}
        for ws_id, regs in self._ws_to_pregs.items():
            ws_reg_usage[ws_id] = {r: 0 for r in regs}

        for vreg in ordered_vregs:
            best_ws = None
            best_reg = None
            best_score = float("inf")


            def_cap = None
            if vreg in self.def_instrs:
                _, _, instr = self.def_instrs[vreg]
                def_cap = self._get_instr_capability(instr)


            capable_ws_ids = set()
            if def_cap:
                capable_ws_ids = set(self._cap_to_ws.get(def_cap, []))

            for ws_id, regs in self._ws_to_pregs.items():
                if capable_ws_ids and ws_id not in capable_ws_ids:
                    continue

                for reg in sorted(regs, key=lambda r: ws_reg_usage[ws_id].get(r, 0)):
                    cap = self._get_preg_capacity(reg)
                    usage = ws_reg_usage[ws_id].get(reg, 0)
                    if cap > 1 or usage == 0:
                        score = usage
                        if score < best_score:
                            best_score = score
                            best_ws = ws_id
                            best_reg = reg
                        break  

            if best_reg is not None and best_ws is not None:
                self.allocation[vreg] = best_reg
                ws_reg_usage[best_ws][best_reg] = ws_reg_usage[best_ws].get(best_reg, 0) + 1
            else:
                self.allocation[vreg] = "memory"

        self._assign_palus()
        return self.allocation


    def allocate(self) -> Dict[str, any]:
        if not self.vregs:
            return {}

        graph = self._build_pbqp()
        solver = PBQPSolver(graph)
        solution, cost = solver.solve()

        vreg_to_id = {v: i for i, v in enumerate(self.vregs)}
        for vreg in self.vregs:
            nid = vreg_to_id[vreg]
            if nid in solution:
                self.allocation[vreg] = self.pregs[solution[nid]]
            else:
                self.allocation[vreg] = "memory"

        self._fix_capacity_violations()
        self._insert_cross_ws_moves()
        self._insert_spill_load_store()
        self._assign_palus()
        return self.allocation

    def get_allocation(self) -> Dict[str, any]:
        return self.allocation


    def _fix_capacity_violations(self):
        """Resolve aggregate capacity overflow left by pairwise PBQP costs through relocation, then memory spill."""
        from collections import defaultdict


        preg_intervals = defaultdict(list)
        for vreg, preg in self.allocation.items():
            if preg == "memory":
                continue
            start, end = self.vreg_liveness.get(vreg, (0, float("inf")))
            preg_intervals[preg].append((vreg, start, end))

        for preg, intervals in preg_intervals.items():
            cap = self._get_preg_capacity(preg)
            if cap <= 1:
                continue  


            events = []
            for vreg, start, end in intervals:
                events.append((start, 1, vreg))
                events.append((end, -1, vreg))
            # Process interval ends before starts at the same time for half-open lifetimes.
            events.sort(key=lambda x: (x[0], x[1]))

            active = set()
            for t, delta, vreg in events:
                if delta == 1:
                    active.add(vreg)
                else:
                    active.discard(vreg)

                if len(active) > cap:

                    spill_candidates = sorted(
                        active,
                        key=lambda v: self.vreg_liveness.get(v, (0, float("inf")))[1]
                                      - self.vreg_liveness.get(v, (0, float("inf")))[0]
                    )
                    excess = len(active) - cap
                    for cand in spill_candidates[:excess]:
                        moved = self._try_relocate_vreg(cand, preg)
                        if not moved:
                            self.allocation[cand] = "memory"
                        active.discard(cand)

    def _try_relocate_vreg(self, vreg: str, current_preg) -> bool:

        ws_id = self._preg_to_ws.get(current_preg)
        if ws_id is None:
            return False


        candidates = sorted(
            [r for r in self._ws_to_pregs.get(ws_id, []) if r != current_preg],
            key=lambda r: self._get_preg_capacity(r),
            reverse=True
        )

        for cand in candidates:
            cap = self._get_preg_capacity(cand)
            if cap <= 1:
                continue
            if self._would_exceed_capacity(vreg, cand):
                continue

            if not self._preg_supports_vreg_capabilities(cand, vreg):
                continue
            self.allocation[vreg] = cand
            return True

        return False

    def _would_exceed_capacity(self, vreg: str, preg) -> bool:

        cap = self._get_preg_capacity(preg)
        if cap <= 1:
            return True

        intervals = []
        for v, p in self.allocation.items():
            if p == preg and v != vreg:
                start, end = self.vreg_liveness.get(v, (0, float("inf")))
                intervals.append((start, end))

        v_start, v_end = self.vreg_liveness.get(vreg, (0, float("inf")))
        intervals.append((v_start, v_end))

        events = []
        for start, end in intervals:
            events.append((start, 1))
            events.append((end, -1))
        events.sort(key=lambda x: (x[0], x[1]))

        active = 0
        max_active = 0
        for t, delta in events:
            active += delta
            max_active = max(max_active, active)

        return max_active > cap

    def _preg_supports_vreg_capabilities(self, preg, vreg: str) -> bool:

        ws_id = self._preg_to_ws.get(preg)
        if ws_id is None:
            return False
        ws_caps = self._ws_capabilities.get(ws_id, set())

        if vreg in self.def_instrs:
            _, _, instr = self.def_instrs[vreg]
            cap = self._get_instr_capability(instr)
            if cap and cap not in ws_caps and not self._is_logistics_cap(cap):
                return False

        for _, _, instr in self.use_instrs.get(vreg, []):
            cap = self._get_instr_capability(instr)
            if cap and cap not in ws_caps and not self._is_logistics_cap(cap):
                return False

        return True

    def _assign_palus(self):
        """Bind each operation to a compatible PALU at its allocated workstation."""
        for block in self.cfg.blocks:
            if self._is_exception_path_block(block):
                continue
            for instr in block.instructions:
                if not isinstance(instr, OpInstr):
                    continue
                key = self.tm.optype_to_instruction(instr.op_type)
                if key is None:
                    instr.assigned_palu_id = 0
                    continue

                exec_ws, exec_preg = self._get_instr_exec_ws_and_preg(instr)
                instr.assigned_workstation_id = exec_ws
                instr.assigned_preg = exec_preg
                if instr.op_type in ("Process", "Inspect", "Adjust") and instr.args:
                    instr.assigned_device_id = instr.args[0]
                palu_id = None
                if exec_ws is not None:
                    palu_id = self._find_palu_for_instr_on_ws(exec_ws, key, instr.rp)

                if palu_id is None:
                    palu_id = self._resolve_palu_id_fallback(key, instr.rp)

                instr.assigned_palu_id = palu_id

    def _find_palu_for_instr_on_ws(self, ws_id: int, key, rp_code: Optional[str]) -> Optional[int]:

        addr = self.tm.data.get("addressing_model", {}) if self.tm and self.tm.data else {}
        ws_map = addr.get("workstation_mapping", [])
        assigned_palus = []
        for entry in ws_map:
            if isinstance(entry, dict) and entry.get("workstation_id") == ws_id:
                assigned_palus = entry.get("assigned_palus", [])
                break

        ics = self.tm.data.get("instruction_capability_set", {}) if self.tm and self.tm.data else {}
        palus = ics.get("palu_definitions", [])

        for palu in palus:
            if not isinstance(palu, dict):
                continue
            palu_id = palu.get("palu_id")
            if palu_id not in assigned_palus:
                continue
            for cap in palu.get("supported_capabilities", []):
                if not isinstance(cap, dict):
                    continue
                if (str(cap.get("opcode", "")) == key.opcode and
                        str(cap.get("func", "")) == key.func):
                    if rp_code and str(cap.get("rp", "")) != rp_code:
                        continue
                    return palu_id
        return None

    def _resolve_palu_id_fallback(self, key, rp_code: Optional[str]) -> int:

        ics = self.tm.data.get("instruction_capability_set", {}) if self.tm and self.tm.data else {}
        palus = ics.get("palu_definitions", [])

        for palu in palus:
            if not isinstance(palu, dict):
                continue
            palu_id = palu.get("palu_id")
            if not isinstance(palu_id, int):
                continue
            for cap in palu.get("supported_capabilities", []):
                if not isinstance(cap, dict):
                    continue
                if (str(cap.get("opcode", "")) == key.opcode and
                        str(cap.get("func", "")) == key.func):
                    if rp_code and str(cap.get("rp", "")) != rp_code:
                        continue
                    return palu_id


        if key.opcode == "000001":
            if key.func == "001":
                return 0
            if key.func == "011":
                return 1
            if key.func == "100":
                return 0
        return 2


    def _insert_cross_ws_moves(self):
        """Insert physical moves when an input material is held outside the operation workstation."""


        for block in self.cfg.blocks:
            if self._is_exception_path_block(block):
                continue

            instr_ws: Dict[Tuple[int, int], int] = {}
            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue
                dominant = None

                if instr.target and self._is_vreg(instr.target):
                    dominant = instr.target
                else:
                    for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                        if i < len(instr.args) and self._is_vreg(instr.args[i]):
                            dominant = instr.args[i]
                            break
                if dominant and dominant in self.allocation:
                    ws = self._preg_to_ws.get(self.allocation[dominant])
                    if ws is not None:
                        instr_ws[(block.id, idx)] = ws

            moves_to_insert: List[Tuple[int, OpInstr]] = []
            local_ws: Dict[str, int] = {}  

            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue
                ws_target = instr_ws.get((block.id, idx))
                if ws_target is None:
                    continue

                for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                    if i >= len(instr.args):
                        continue
                    vreg = instr.args[i]
                    if not self._is_vreg(vreg):
                        continue

                    preg = self.allocation.get(vreg)
                    if preg is None:
                        continue

                    ws_src = self._preg_to_ws.get(preg)
                    effective_ws = local_ws.get(vreg, ws_src)

                    if effective_ws is not None and effective_ws != ws_target:
                        # Use the output allocation as the transfer destination when available.


                        dst_reg = None
                        if instr.target and self._is_vreg(instr.target):
                            dst_reg = self.allocation.get(instr.target)
                            if dst_reg == "memory":
                                dst_reg = None
                        if dst_reg is None:
                            dominant_preg = None
                            if instr.target and self._is_vreg(instr.target):
                                dominant_preg = self.allocation.get(instr.target)
                            dst_reg = self._select_spill_register(dominant_preg, ws_target)
                            if dst_reg is None:
                                dst_pregs = self._ws_to_pregs.get(ws_target, [])
                                if dst_pregs:
                                    dst_reg = dst_pregs[0]
                        if dst_reg is not None:
                            move_instr = OpInstr(
                                op_type="Move",
                                target=None,
                                args=[vreg, str(dst_reg)],
                            )
                            moves_to_insert.append((idx, move_instr))
                            local_ws[vreg] = ws_target


            moves_to_insert.sort(key=lambda x: x[0], reverse=True)
            for insert_idx, move_instr in moves_to_insert:
                block.instructions.insert(insert_idx, move_instr)


    def _insert_spill_load_store(self):
        """Insert Load/Store operations for spilled versions using nearby temporary registers."""
        for block in self.cfg.blocks:
            if self._is_exception_path_block(block):
                continue
            loads_to_insert: List[Tuple[int, OpInstr]] = []
            stores_to_insert: List[Tuple[int, OpInstr]] = []
            # Reuse material loaded into a temporary register within this block.
            block_loaded: Dict[str, int] = {}

            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue

                exec_ws, exec_preg = self._get_instr_exec_ws_and_preg(instr)
                if exec_ws is None:
                    continue


                out_vregs = []
                if instr.target and self._is_vreg(instr.target):
                    out_vregs.append(instr.target)
                for it in instr.implicit_targets or []:
                    if self._is_vreg(it):
                        out_vregs.append(it)

                for vreg in out_vregs:
                    if self.allocation.get(vreg) == "memory":
                        src_preg = self._select_spill_register(exec_preg, exec_ws)
                        if src_preg is not None:
                            store_instr = OpInstr(
                                op_type="Store",
                                target=None,
                                args=[vreg, str(src_preg)],
                            )
                            stores_to_insert.append((idx + 1, store_instr))

                            if vreg in block_loaded:
                                del block_loaded[vreg]


                in_idxs = self._material_arg_indices(instr.op_type, len(instr.args))
                for i in in_idxs:
                    if i >= len(instr.args):
                        continue
                    vreg = instr.args[i]
                    if not self._is_vreg(vreg):
                        continue
                    if self.allocation.get(vreg) != "memory":
                        continue


                    if vreg in block_loaded:
                        continue

                    dst_preg = self._select_spill_register(exec_preg, exec_ws)
                    if dst_preg is not None:
                        load_instr = OpInstr(
                            op_type="Load",
                            target=None,
                            args=[vreg, str(dst_preg)],
                        )
                        loads_to_insert.append((idx, load_instr))
                        block_loaded[vreg] = dst_preg


            stores_to_insert.sort(key=lambda x: x[0], reverse=True)
            loads_to_insert.sort(key=lambda x: x[0], reverse=True)
            for insert_idx, instr in stores_to_insert:
                block.instructions.insert(insert_idx, instr)
            for insert_idx, instr in loads_to_insert:
                block.instructions.insert(insert_idx, instr)
