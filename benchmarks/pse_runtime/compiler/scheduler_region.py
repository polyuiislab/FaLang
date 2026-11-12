# F_IDE_Project/compiler/scheduler_region.py
"""
FaLang 指令调度入口。

功能：
1. 按 CFG 拓扑序遍历基本块，逐块调用列表调度器。
2. 块间通过 ResourceState 快照保守传递（汇合点取最保守状态）。
3. 支持 runtime_state_override 注入 JIT 编译时的初始资源状态。
"""

from typing import Dict, List, Optional, Tuple

from compiler.ir import BasicBlock, ControlFlowGraph, OpInstr
from compiler.mdg import ManufacturingDependencyGraph, MDGNode
from compiler.scheduler import ListScheduler, ResourceState, ScheduleResult


class RegionScheduler:
    """逐块列表调度入口（MFR 区域调度已删除，仅保留逐块调度）。"""

    def __init__(self, target_model=None, scheduling_tick: float = 0.0):
        self.tm = target_model
        self.tick = scheduling_tick

    # ------------------------------------------------------------------
    # 状态合并（保守交集）：多前驱汇入时取最保守状态
    # ------------------------------------------------------------------
    def _merge_states(self, states: List[ResourceState]) -> ResourceState:
        """多前驱汇入时，取最保守状态。"""
        merged = ResourceState()
        if not states:
            return merged

        # PALU 实例状态合并：取每个实例的最晚 busy_until
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

        # 物料状态：若所有前驱一致则保留，否则标记为 Unknown（ready_time 取 max）
        all_mats = set()
        for s in states:
            all_mats.update(s.material_status.keys())
        for mat in all_mats:
            statuses = [s.material_status.get(mat) for s in states]
            if all(st == statuses[0] for st in statuses):
                merged.material_status[mat] = statuses[0]
            else:
                # 保守：认为物料需要重新定位，ready_time 取最晚
                max_ready = max((st[2] if st else 0.0) for st in statuses)
                merged.material_status[mat] = (None, None, max_ready)

        # 缓存占用取并集（最坏情况）
        for s in states:
            for lvl, mats in s.cache_occupancy.items():
                merged.cache_occupancy.setdefault(lvl, set()).update(mats)

        # 容量
        for s in states:
            merged.cache_capacity.update(s.cache_capacity)

        # Device last model: 取最保守（任意一个都可）
        for s in states:
            merged.device_last_model.update(s.device_last_model)

        return merged

    # ------------------------------------------------------------------
    # 主入口：逐块列表调度
    # ------------------------------------------------------------------
    def schedule(
        self,
        cfg: ControlFlowGraph,
        mdg: ManufacturingDependencyGraph,
        initial_state_override: Optional[Dict] = None,
        device_last_model_override: Optional[Dict[str, str]] = None,
    ) -> ScheduleResult:
        """
        对 CFG 进行逐块指令调度（不再执行 MFR 区域合并）。

        initial_state_override: 可选运行时状态字典，注入到调度器的初始 ResourceState。
        device_last_model_override: 可选设备最后处理的 model 追踪信息。
        """
        list_scheduler = ListScheduler(mdg, cfg, target_model=self.tm, scheduling_tick=self.tick)
        if device_last_model_override:
            list_scheduler._device_last_model = dict(device_last_model_override)

        block_schedules: Dict[int, List[Tuple[MDGNode, float, float]]] = {}
        exit_states: Dict[int, ResourceState] = {}
        device_timeline: Dict[str, List[Tuple[float, float, str]]] = {}
        material_timeline: Dict[str, List[Tuple[float, float, str]]] = {}
        cache_peak: Dict[str, int] = {}

        # 计算块拓扑序（Kahn）
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

        # 第一个块使用 override 的初始状态
        first_block_initial = None
        if initial_state_override is not None:
            first_block_initial = list_scheduler.override_initial_state(initial_state_override)

        for block in topo:
            # 确定初始状态：单前驱直接传递；多前驱保守合并
            preds = [p for p in block.predecessors if p.id in exit_states]
            if preds:
                initial_state = self._merge_states([exit_states[p.id] for p in preds])
            elif first_block_initial is not None and block == topo[0]:
                initial_state = first_block_initial
            else:
                initial_state = ResourceState()
                initial_state.cache_capacity = dict(list_scheduler.cache_capacity)

            # 逐块列表调度（不再执行 MFR 区域合并）
            sched, exit_state = list_scheduler.schedule_block(block, initial_state)
            block_schedules[block.id] = sched
            if hasattr(block, "schedule_result"):
                block.schedule_result = sched
            exit_states[block.id] = exit_state

            # 收集统计信息
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

        # 每个基本块视为独立的 1-block region（兼容旧接口）
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
