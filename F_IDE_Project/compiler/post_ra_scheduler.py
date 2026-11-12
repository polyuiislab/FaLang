"""Coalesce moves and reschedule within each block using physical-register dependencies; terminators remain last."""
from typing import Dict, List, Optional, Set, Tuple

from compiler.ir import OpInstr, BranchInstr, JumpInstr, BasicBlock, ControlFlowGraph
from core.target_model import TargetModelLoader


class PostRAScheduler:
    def __init__(self, target_model: TargetModelLoader):
        self.tm = target_model
        self.resource_names: Set[str] = set()


    def schedule(self, cfg: ControlFlowGraph, allocation: Dict[str, any]):

        self.resource_names = set(getattr(cfg, "resource_names", set()) or set())
        for block in cfg.blocks:
            self._coalesce_block(block, allocation)
            self._schedule_block(block, allocation)


    def _coalesce_block(self, block: BasicBlock, allocation: Dict[str, any]):
        new_instrs = []
        for instr in block.instructions:
            if isinstance(instr, OpInstr) and instr.op_type in ("Assign", "Move"):
                if instr.target and self._is_vreg(instr.target):
                    src = instr.args[0] if instr.args else None
                    if src and self._is_vreg(src):
                        if allocation.get(src) == allocation.get(instr.target):

                            continue
            new_instrs.append(instr)
        block.instructions = new_instrs


    def _schedule_block(self, block: BasicBlock, allocation: Dict[str, any]):

        phys_instrs: List[OpInstr] = []
        ctrl_instrs = []
        for instr in block.instructions:
            if isinstance(instr, (BranchInstr, JumpInstr)):
                ctrl_instrs.append(instr)
            else:
                phys_instrs.append(instr)

        if len(phys_instrs) <= 1:
            block.instructions = phys_instrs + ctrl_instrs
            return

        adj, in_deg = self._build_dag(phys_instrs, allocation)
        heights = self._compute_heights(adj, phys_instrs)
        scheduled = self._list_schedule(adj, in_deg, phys_instrs, heights)
        block.instructions = scheduled + ctrl_instrs


    def _precompute_capacity_conflicts(
        self, instrs: List[OpInstr], allocation: Dict[str, any]
    ) -> Set[any]:
        """Detect capacity conflicts from scheduled holding intervals; untimed operations count as conflicting."""
        conflicts: Set[any] = set()

        preg_entries: Dict[any, List[dict]] = {}

        for instr in instrs:
            if not isinstance(instr, OpInstr):
                continue

            if instr.target and self._is_vreg(instr.target):
                preg = allocation.get(instr.target)
                if preg and preg != "memory":
                    found = False
                    for entry in preg_entries.get(preg, []):
                        if entry["vreg"] == instr.target:
                            entry["def"] = instr
                            found = True
                            break
                    if not found:
                        preg_entries.setdefault(preg, []).append(
                            {"vreg": instr.target, "def": instr, "uses": []}
                        )

            for i in self._material_arg_indices(instr.op_type, len(instr.args)):
                if i < len(instr.args):
                    vreg = instr.args[i]
                    if self._is_vreg(vreg):
                        preg = allocation.get(vreg)
                        if preg and preg != "memory":
                            for entry in preg_entries.get(preg, []):
                                if entry["vreg"] == vreg:
                                    entry["uses"].append(instr)
                                    break

        for preg, entries in preg_entries.items():
            cap = self._get_preg_capacity(preg)
            if cap >= 100:
                continue

            intervals: List[Tuple[float, float]] = []
            has_missing_time = False

            for entry in entries:
                def_instr = entry["def"]
                uses = entry["uses"]

                def_start = getattr(def_instr, "scheduled_start", None)
                def_end = getattr(def_instr, "scheduled_finish", None)
                if def_start is None or def_end is None:
                    has_missing_time = True
                    break

                last_end = def_end
                for use in uses:
                    use_end = getattr(use, "scheduled_finish", None)
                    if use_end is None:
                        has_missing_time = True
                        break
                    if use_end > last_end:
                        last_end = use_end

                if has_missing_time:
                    break
                intervals.append((def_start, last_end))

            if has_missing_time:
                conflicts.add(preg)
                continue


            events = []
            for s, e in intervals:
                events.append((s, 1))
                events.append((e, -1))
            events.sort()

            max_occ = 0
            curr = 0
            for _, delta in events:
                curr += delta
                max_occ = max(max_occ, curr)

            if max_occ > cap:
                conflicts.add(preg)

        return conflicts


    def _build_dag(self, instrs: List[OpInstr], allocation: Dict[str, any]):
        n = len(instrs)
        adj: List[List[Tuple[int, float]]] = [[] for _ in range(n)]
        in_deg = [0] * n

        conflicts = self._precompute_capacity_conflicts(instrs, allocation)

        write_of: Dict[any, int] = {}
        read_of: Dict[any, List[int]] = {}
        device_of: Dict[str, int] = {}
        mem_of: Dict[str, int] = {}

        for j, instr in enumerate(instrs):
            if not isinstance(instr, OpInstr):
                continue

            reads = self._get_instr_reads(instr, allocation)
            writes = self._get_instr_writes(instr, allocation)
            dev = self._get_device(instr)
            mem_vreg = self._get_memory_vreg(instr)

            # Read-after-write dependency.
            for preg in reads:
                if preg in write_of:
                    i = write_of[preg]
                    w = self._compute_edge_weight(i, j, instrs, allocation, "raw")
                    adj[i].append((j, w))
                    in_deg[j] += 1

            # Write-after-read ordering applies to registers with capacity conflicts.
            for preg in writes:
                if preg in conflicts and preg in read_of:
                    for i in read_of[preg]:
                        w = self._compute_edge_weight(i, j, instrs, allocation, "war")
                        adj[i].append((j, w))
                        in_deg[j] += 1

            # Write-after-write ordering applies to registers with capacity conflicts.
            for preg in writes:
                if preg in conflicts and preg in write_of:
                    i = write_of[preg]
                    w = self._compute_edge_weight(i, j, instrs, allocation, "waw")
                    adj[i].append((j, w))
                    in_deg[j] += 1


            if dev and dev in device_of:
                i = device_of[dev]
                w = self._compute_edge_weight(i, j, instrs, allocation, "struct")
                adj[i].append((j, w))
                in_deg[j] += 1


            if mem_vreg:
                mem_key = f"mem_{mem_vreg}"
                if mem_key in mem_of:
                    i = mem_of[mem_key]
                    w = self._compute_edge_weight(i, j, instrs, allocation, "mem")
                    adj[i].append((j, w))
                    in_deg[j] += 1
                mem_of[mem_key] = j


            for preg in writes:
                write_of[preg] = j
                read_of.pop(preg, None)
            for preg in reads:
                read_of.setdefault(preg, []).append(j)
            if dev:
                device_of[dev] = j

        return adj, in_deg

    def _compute_edge_weight(
        self, i: int, j: int, instrs: List[OpInstr], allocation: Dict[str, any], edge_type: str
    ) -> float:
        """Include cross-register logistics time in the dependency weight."""
        i_instr = instrs[i]


        if isinstance(i_instr, OpInstr) and i_instr.op_type in ("Move", "Assign"):
            lt = self._get_move_logistics_time(i_instr, allocation)
            if lt is not None:
                return lt

        base = self._get_latency(i_instr)


        if edge_type in ("raw", "waw", "war"):
            i_preg = self._get_instr_dominant_preg(i_instr, allocation)
            j_preg = self._get_instr_dominant_preg(instrs[j], allocation)
            if (i_preg is not None and j_preg is not None and
                i_preg != j_preg and i_preg != "memory" and j_preg != "memory"):
                lt = self.tm.get_logistics_time(i_preg, j_preg) if self.tm else None
                if lt is not None:
                    base += lt

        return base

    def _get_move_logistics_time(self, instr: OpInstr, allocation: Dict[str, any]) -> Optional[float]:

        if instr.op_type not in ("Move", "Assign"):
            return None
        if not instr.args or len(instr.args) < 2:
            return None
        vreg = instr.args[0]
        dst_str = instr.args[1]
        if not isinstance(vreg, str) or not isinstance(dst_str, str):
            return None
        src_preg = allocation.get(vreg)
        try:
            dst_preg = int(dst_str)
        except ValueError:
            return None
        if src_preg is None or src_preg == "memory":
            return None
        if self.tm:
            lt = self.tm.get_logistics_time(src_preg, dst_preg)
            if lt is not None:
                return lt
        return None

    def _get_instr_dominant_preg(self, instr: OpInstr, allocation: Dict[str, any]) -> Optional[any]:

        if not isinstance(instr, OpInstr):
            return None
        if instr.target and self._is_vreg(instr.target):
            return allocation.get(instr.target)
        for idx in self._material_arg_indices(instr.op_type, len(instr.args)):
            if idx < len(instr.args) and self._is_vreg(instr.args[idx]):
                return allocation.get(instr.args[idx])
        return None


    def _compute_heights(self, adj: List[List[Tuple[int, float]]], instrs: List[OpInstr]) -> List[float]:
        n = len(instrs)
        heights = [0.0] * n

        def dfs(u: int) -> float:
            if heights[u] != 0.0:
                return heights[u]
            max_h = 0.0
            for v, w in adj[u]:
                h = dfs(v) + w
                if h > max_h:
                    max_h = h
            heights[u] = max_h
            return max_h

        for u in range(n):
            if heights[u] == 0.0:
                dfs(u)
        return heights


    def _list_schedule(
        self,
        adj: List[List[Tuple[int, float]]],
        in_deg: List[int],
        instrs: List[OpInstr],
        heights: List[float],
    ) -> List[OpInstr]:
        n = len(instrs)
        indeg = list(in_deg)
        ready = [i for i in range(n) if indeg[i] == 0]

        scheduled: List[OpInstr] = []
        active: Dict[int, float] = {}
        current_time = 0.0
        device_busy_until: Dict[str, float] = {}

        while ready or active:
            candidates = []
            for i in ready:
                instr = instrs[i]
                dev = self._get_device(instr)
                if dev and device_busy_until.get(dev, 0.0) > current_time:
                    continue
                candidates.append(i)

            if candidates:
                def priority(idx: int) -> float:
                    p = heights[idx]
                    instr = instrs[idx]
                    if isinstance(instr, OpInstr):
                        if instr.op_type == "Load":
                            p += 10.0
                        elif instr.op_type == "Store":
                            p -= 2.0
                        elif instr.op_type in ("Move", "Assign"):
                            p -= 1.0
                    return p

                candidates.sort(key=priority, reverse=True)
                chosen = candidates[0]
                instr = instrs[chosen]
                latency = self._get_latency(instr)

                dev = self._get_device(instr)
                if dev:
                    device_busy_until[dev] = current_time + latency

                active[chosen] = current_time + latency
                ready.remove(chosen)
                scheduled.append(instr)

            if not active and not ready:
                break

            if not active:
                if candidates:
                    earliest = float('inf')
                    for i in ready:
                        dev = self._get_device(instrs[i])
                        if dev:
                            t = device_busy_until.get(dev, 0.0)
                            if t < earliest:
                                earliest = t
                    if earliest < float('inf') and earliest > current_time:
                        current_time = earliest
                        continue
                break

            next_time = min(active.values())
            finished = [idx for idx, ft in list(active.items()) if ft <= next_time]
            for idx in finished:
                del active[idx]
                for v, _w in adj[idx]:
                    indeg[v] -= 1
                    if indeg[v] == 0:
                        ready.append(v)

            current_time = next_time

        return scheduled


    def _is_vreg(self, name: str) -> bool:
        if not isinstance(name, str) or not name:
            return False
        if name.isdigit() or name.startswith('"'):
            return False
        if name in self.resource_names:
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

    def _get_instr_reads(self, instr: OpInstr, allocation: Dict[str, any]) -> Set[any]:
        reads = set()
        for i in self._material_arg_indices(instr.op_type, len(instr.args)):
            if i < len(instr.args):
                vreg = instr.args[i]
                if self._is_vreg(vreg):
                    preg = allocation.get(vreg)
                    if preg and preg != "memory":
                        reads.add(preg)
        return reads

    def _get_instr_writes(self, instr: OpInstr, allocation: Dict[str, any]) -> Set[any]:
        writes = set()
        if instr.target and self._is_vreg(instr.target):
            preg = allocation.get(instr.target)
            if preg and preg != "memory":
                writes.add(preg)
        for it in instr.implicit_targets or []:
            if self._is_vreg(it):
                preg = allocation.get(it)
                if preg and preg != "memory":
                    writes.add(preg)
        return writes

    def _get_device(self, instr: OpInstr) -> Optional[str]:
        if not isinstance(instr, OpInstr):
            return None
        if instr.op_type in ("Process", "Inspect", "Adjust"):
            if instr.args and instr.args[0] not in self.resource_names:
                return None
            if instr.args:
                return instr.args[0]
        return None

    def _get_memory_vreg(self, instr: OpInstr) -> Optional[str]:
        if not isinstance(instr, OpInstr):
            return None
        if instr.op_type in ("Load", "Store"):
            idxs = self._material_arg_indices(instr.op_type, len(instr.args))
            for i in idxs:
                if i < len(instr.args):
                    return instr.args[i]
        return None

    def _get_preg_capacity(self, preg) -> int:
        if preg == "memory":
            return 10000
        storage_model = self.tm.data.get("storage_model", {}) if self.tm and self.tm.data else {}
        for entry in storage_model.get("capacity_constraints", []):
            if entry.get("storage_id") == preg:
                return entry.get("capacity", 1)
        return 1

    def _get_latency(self, instr: OpInstr) -> float:
        if not isinstance(instr, OpInstr):
            return 0.0
        if instr.expected_time is not None:
            return float(instr.expected_time)
        if self.tm:
            if instr.op_type == "Load":
                return self.tm.get_default_load_time()
            if instr.op_type == "Store":
                return self.tm.get_default_store_time()
            if instr.op_type in ("Move", "Assign"):
                return self.tm.get_default_move_time()
            key = self.tm.optype_to_instruction(instr.op_type)
            if key and instr.opcode and instr.func and instr.rp:
                t = self.tm.get_expected_time(instr.opcode, instr.func, instr.rp)
                if t is not None:
                    return float(t)
        return 1.0
