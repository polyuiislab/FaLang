"""Schedule blocks in CFG order and propagate resource snapshots across control-flow edges."""

from typing import Dict, List, Optional, Tuple

from compiler.ir import BasicBlock, ControlFlowGraph, OpInstr
from compiler.mdg import ManufacturingDependencyGraph, MDGNode
from compiler.scheduler import ListScheduler, ResourceState, ScheduleResult


class RegionScheduler:


    def __init__(self, target_model=None, scheduling_tick: float = 0.0):
        self.tm = target_model
        self.tick = scheduling_tick


    def _merge_states(self, states: List[ResourceState]) -> ResourceState:
        """Merge predecessor snapshots using latest availability times and combined occupancy."""
        merged = ResourceState()
        if not states:
            return merged


        all_palu_ids: Set[int] = set()
        for s in states:
            all_palu_ids.update(s.palu_instances.keys())
        for pid in all_palu_ids:
            instances_lists = [s.palu_instances.get(pid, []) for s in states]
            if not instances_lists or not instances_lists[0]:
                continue
            merged_instances = []
            n = len(instances_lists[0])
            for idx in range(n):
                merged_instances.append(
                    max((lst[idx] if idx < len(lst) else 0.0) for lst in instances_lists)
                )
            merged.palu_instances[pid] = merged_instances

        # Retain a location only when predecessor snapshots agree; use the latest ready time.
        all_mats = set()
        for s in states:
            all_mats.update(s.material_status.keys())
        for mat in all_mats:
            statuses = [s.material_status.get(mat) for s in states]
            if all(st == statuses[0] for st in statuses):
                merged.material_status[mat] = statuses[0]
            else:

                max_ready = max((st[2] if st else 0.0) for st in statuses)
                merged.material_status[mat] = (None, None, max_ready)

        # The merged occupancy contains material present on any incoming path.
        for s in states:
            for lvl, mats in s.cache_occupancy.items():
                merged.cache_occupancy.setdefault(lvl, set()).update(mats)


        for s in states:
            merged.cache_capacity.update(s.cache_capacity)


        for s in states:
            merged.device_last_model.update(s.device_last_model)

        return merged


    def schedule(
        self,
        cfg: ControlFlowGraph,
        mdg: ManufacturingDependencyGraph,
        initial_state_override: Optional[Dict] = None,
        device_last_model_override: Optional[Dict[str, str]] = None,
    ) -> ScheduleResult:
        """Schedule each block, optionally starting from a runtime state and device setup history."""
        list_scheduler = ListScheduler(mdg, cfg, target_model=self.tm, scheduling_tick=self.tick)
        if device_last_model_override:
            list_scheduler._device_last_model = dict(device_last_model_override)

        block_schedules: Dict[int, List[Tuple[MDGNode, float, float]]] = {}
        exit_states: Dict[int, ResourceState] = {}
        device_timeline: Dict[str, List[Tuple[float, float, str]]] = {}
        material_timeline: Dict[str, List[Tuple[float, float, str]]] = {}
        cache_peak: Dict[str, int] = {}


        in_deg = {b.id: len(b.predecessors) for b in cfg.blocks}
        queue = [b for b in cfg.blocks if in_deg[b.id] == 0]
        topo: List[BasicBlock] = []
        while queue:
            b = queue.pop(0)
            topo.append(b)
            for succ in b.successors:
                in_deg[succ.id] -= 1
                if in_deg[succ.id] == 0:
                    queue.append(succ)


        first_block_initial = None
        if initial_state_override is not None:
            first_block_initial = list_scheduler.override_initial_state(initial_state_override)

        for block in topo:

            preds = [p for p in block.predecessors if p.id in exit_states]
            if preds:
                initial_state = self._merge_states([exit_states[p.id] for p in preds])
            elif first_block_initial is not None and block == topo[0]:
                initial_state = first_block_initial
            else:
                initial_state = ResourceState()
                initial_state.cache_capacity = dict(list_scheduler.cache_capacity)


            sched, exit_state = list_scheduler.schedule_block(block, initial_state)
            block_schedules[block.id] = sched
            if hasattr(block, "schedule_result"):
                block.schedule_result = sched
            exit_states[block.id] = exit_state


            for node, st, fin in sched:
                dev = list_scheduler._get_device(node)
                if dev:
                    device_timeline.setdefault(dev, []).append((st, fin, str(node.instr)))
                for mat in list_scheduler._get_input_materials(node):
                    material_timeline.setdefault(mat, []).append((st, fin, str(node.instr)))
                for lvl, mats in exit_state.cache_occupancy.items():
                    peak = len(mats)
                    if peak > cache_peak.get(lvl, 0):
                        cache_peak[lvl] = peak

        global_makespan = 0.0
        for sched in block_schedules.values():
            if sched:
                global_makespan = max(global_makespan, max(fin for _, _, fin in sched))


        mfr_boundaries = [(b.id,) for b in cfg.blocks]

        return ScheduleResult(
            block_schedules=block_schedules,
            global_makespan=global_makespan,
            device_timeline=device_timeline,
            material_timeline=material_timeline,
            cache_peak=cache_peak,
            mfr_boundaries=mfr_boundaries,
            exit_states=exit_states,
        )
