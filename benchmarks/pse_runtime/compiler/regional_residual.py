# F_IDE_Project/compiler/regional_residual.py
"""
Paper 1 —— Precompiled Exception Handling、Regional Residual Recompilation 与二者的交界。

制造语境：设备停机的时候，车间不会把整张订单推倒重排。做完的工序不会倒退，正在做的工序
不能打断，真正需要重新决定的，只是"受这次停机波及、且还没开始"的那一小块物料该放在哪里。

但停机并不必然要重排。如果订单程序里已经预定义了处置——比如把这道工序在同类工位上重做
一遍——那么先按预定义的办法办；只有当预定义的处置**兜不住**（处置完之后，处置链条之外的
物料版本连一个可行位置都没有了）时，才需要对那一部分做区域性重新落位。

判据是事件在上下文中的三个属性，不是事件种类：

    anticipation            设计时：这个程序点上有没有预定义处理器
    executability at trigger 触发时：处理器的动作集此刻拿不拿得到物理资源
    containment              触发后：处理器链条之外的版本，候选集是否仍旧有效

`containment` 把"区域边界"从启发式变成可计算判据：区域由**候选集被实际作废的版本**定义，
而不是由时间窗或机器组切出来。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from compiler.factory_target_machine import (
    AVAIL_HOLDING,
    AVAIL_LOGISTICS,
    AVAIL_WORKSTATION,
    FactoryTargetMachine,
)
from compiler.ir import OpInstr
from compiler.residual_ir import RESIDUAL_PENDING, ResidualWorkset
from compiler.residual_coloring import WorksetColorer, WorksetColorResult
from compiler.target_lowering import LoweringResult
from compiler.uncertainty import (
    TARGET_HOLDING,
    TARGET_LOGISTICS,
    TARGET_MATERIAL,
    TARGET_WORKSTATION,
    EventAttributes,
    HandlingPlan,
    ResponsePolicy,
    UncertaintyEvent,
)


@dataclass
class AffectedRegion:
    """一次事件波及到的残余区域。"""

    event: UncertaintyEvent
    directly_affected: Set[str] = field(default_factory=set)
    expansion: Set[str] = field(default_factory=set)
    by_order: Dict[str, Set[str]] = field(default_factory=dict)
    frozen: Set[str] = field(default_factory=set)
    expands_to_orders: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.expansion

    def snapshot(self) -> Dict[str, object]:
        return {
            "event": self.event.snapshot(),
            "directly_affected": len(self.directly_affected),
            "expansion": len(self.expansion),
            "orders": len(self.by_order),
            "frozen": len(self.frozen),
        }


@dataclass
class RecompileResult:
    region: AffectedRegion
    color_result: Optional[WorksetColorResult] = None
    rebuilt: bool = False
    reason: str = ""

    def snapshot(self) -> Dict[str, object]:
        return {
            "rebuilt": self.rebuilt,
            "reason": self.reason,
            "region": self.region.snapshot(),
            "coloring": self.color_result.snapshot() if self.color_result else None,
        }


class RegionalResidualRecompilation:
    """受影响区域识别 + 区域性重新落位。"""

    def __init__(
        self,
        f_t: FactoryTargetMachine,
        lowerings: Mapping[str, LoweringResult],
        max_hops: int = 0,
    ):
        self.f_t = f_t
        self.lowerings = dict(lowerings)
        self.max_hops = int(max_hops)
        self._adjacency_cache: Dict[str, Dict[str, Set[str]]] = {}

    # ------------------------------------------------------------------
    # 1. 事件落到 F_t 上
    # ------------------------------------------------------------------
    def apply_state_change(self, event: UncertaintyEvent, factory_state=None) -> Optional[float]:
        """把会作废资源的事件写成 F_t 的可用性变化。"""
        if not event.type.invalidates_resource or event.resolves_at is None:
            return None
        state = factory_state if factory_state is not None else self.f_t.state

        pair = self._availability_pair(event)
        if pair is None:
            return None
        state.unavailable_until[pair] = float(event.resolves_at)
        return float(event.resolves_at)

    def release_state_change(self, event: UncertaintyEvent, factory_state=None) -> bool:
        state = factory_state if factory_state is not None else self.f_t.state
        pair = self._availability_pair(event)
        if pair is None:
            return False
        return state.unavailable_until.pop(pair, None) is not None

    @staticmethod
    def _availability_pair(event: UncertaintyEvent) -> Optional[Tuple[str, object]]:
        if event.target is None:
            return None
        if event.target_kind == TARGET_WORKSTATION:
            return (AVAIL_WORKSTATION, event.target)
        if event.target_kind == TARGET_HOLDING:
            return (AVAIL_HOLDING, event.target)
        if event.target_kind == TARGET_LOGISTICS:
            return (AVAIL_LOGISTICS, event.target)
        return None

    # ------------------------------------------------------------------
    # 2. 受影响区域识别
    # ------------------------------------------------------------------
    def detect_region(
        self,
        event: UncertaintyEvent,
        workset: ResidualWorkset,
        allocation: Mapping[str, int],
        current_time: float = 0.0,
        seeds: Optional[Set[str]] = None,
    ) -> AffectedRegion:
        """
        提取受影响区域。

        `seeds=None` 时，按事件直接作废的资源确定种子；空集合表示区域为空。
        给出 `seeds`（例如"处理器兜不住的那些版本"）时，以它们为种子做有界扩张。
        """
        region = AffectedRegion(event=event)
        if workset is None or not workset.fragments:
            return region

        if seeds is None:
            if not event.type.invalidates_resource:
                return region
            invalid_ws, invalid_sites, logistics_down = self._invalidated_resources(
                event, current_time
            )
            seeds = set()
            for fragment in workset.fragments:
                lowering = self.lowerings.get(fragment.order_id)
                if lowering is None:
                    continue
                for vreg in set(fragment.pending_vregs) | set(fragment.carried_vregs):
                    if self._recovers_before_use(event, workset, vreg):
                        continue
                    site = allocation.get(vreg)
                    if self._is_directly_invalidated(vreg, site, lowering, invalid_ws,
                                                     invalid_sites, logistics_down):
                        seeds.add(vreg)
                        region.by_order.setdefault(fragment.order_id, set()).add(vreg)
            region.directly_affected = set(seeds)
        else:
            seeds = set(seeds)
            for fragment in workset.fragments:
                owned = seeds & (set(fragment.pending_vregs) | set(fragment.carried_vregs))
                if owned:
                    region.by_order[fragment.order_id] = owned
            region.directly_affected = set(seeds)

        expansion: Dict[str, Set[str]] = {}
        for fragment in workset.fragments:
            fragment_seeds = set(region.by_order.get(fragment.order_id, set()))
            if not fragment_seeds:
                continue
            # 正在执行的版本属于 carried，只保留其现场位置。
            allowed = set(fragment.pending_vregs)
            reached = self._expand(fragment.order_id, fragment.mdg, fragment_seeds, allowed)
            if reached:
                expansion[fragment.order_id] = reached
                region.by_order[fragment.order_id] = reached
            else:
                region.by_order.pop(fragment.order_id, None)

        region.expansion = {v for group in expansion.values() for v in group}
        region.expands_to_orders = sorted(expansion)
        all_residual = set(workset.pending_vregs) | set(workset.carried_vregs)
        region.frozen = all_residual - region.expansion
        return region

    def versions_with_invalidated_candidates(
        self,
        event: UncertaintyEvent,
        workset: ResidualWorkset,
        current_time: float = 0.0,
        exclude: Optional[Set[str]] = None,
        allocation: Optional[Mapping[str, int]] = None,
    ) -> Set[str]:
        """
        候选集被事件实际作废的物料版本（可排除处理器链条内的版本）。

        已绑定版本检查其实际落点；尚无绑定的版本检查候选集合是否已失效。
        """
        exclude = exclude or set()
        invalid_ws, invalid_sites, logistics_down = self._invalidated_resources(event, current_time)
        if not invalid_ws and not invalid_sites and not logistics_down:
            return set()

        affected: Set[str] = set()
        for fragment in workset.fragments:
            lowering = self.lowerings.get(fragment.order_id)
            if lowering is None:
                continue
            for vreg in set(fragment.pending_vregs) | set(fragment.carried_vregs):
                if vreg in exclude:
                    continue
                if self._recovers_before_use(event, workset, vreg):
                    continue
                if allocation is not None and vreg in allocation:
                    if self._is_directly_invalidated(vreg, allocation[vreg], lowering,
                                                     invalid_ws, invalid_sites, logistics_down):
                        affected.add(vreg)
                    continue
                sites = lowering.eligible_sites.get(vreg) or []
                workstations = {
                    self.f_t.holding_site(site).workstation_id
                    for site in sites if self.f_t.holding_site(site) is not None
                }
                if workstations and workstations.issubset(invalid_ws):
                    affected.add(vreg)
                    continue
                if invalid_sites and sites and set(sites).issubset(invalid_sites):
                    affected.add(vreg)
                    continue
                if logistics_down and sites and all(
                        self._requires_transport(vreg, site, lowering) for site in sites):
                    affected.add(vreg)
        return affected

    @staticmethod
    def _recovers_before_use(event, workset, vreg):
        """资源在工序最早开工前已恢复，原绑定仍然可执行。"""
        return (event.resolves_at is not None and vreg in workset.earliest_use
                and workset.earliest_use[vreg] >= event.resolves_at)

    def _invalidated_resources(
        self, event: UncertaintyEvent, current_time: float
    ) -> Tuple[Set[int], Set[int], bool]:
        invalid_ws: Set[int] = set()
        derived_sites: Set[int] = set()
        own_sites: Set[int] = set()
        logistics_down = False

        if event.target_kind == TARGET_WORKSTATION and event.target is not None:
            ws_id = int(event.target)
            if not self._has_recovered(AVAIL_WORKSTATION, ws_id, current_time):
                invalid_ws.add(ws_id)
                derived_sites.update(self.f_t.holding_sites_of_workstation(ws_id))
        elif event.target_kind == TARGET_HOLDING and event.target is not None:
            site_id = int(event.target)
            if not self._has_recovered(AVAIL_HOLDING, site_id, current_time):
                own_sites.add(site_id)
        elif event.target_kind == TARGET_LOGISTICS:
            logistics = self.f_t.logistics_resource()
            if logistics is not None and not self._has_recovered(
                    AVAIL_LOGISTICS, logistics.palu_id, current_time):
                logistics_down = True

        return invalid_ws, derived_sites | own_sites, logistics_down

    def _has_recovered(self, kind: str, key: object, current_time: float) -> bool:
        until = self.f_t.state.unavailable_until.get((kind, key))
        return until is not None and current_time >= until

    def _is_directly_invalidated(
        self,
        vreg: str,
        site: Optional[int],
        lowering: LoweringResult,
        invalid_ws: Set[int],
        invalid_sites: Set[int],
        logistics_down: bool,
    ) -> bool:
        if isinstance(site, int):
            if site in invalid_sites:
                return True
            holding = self.f_t.holding_site(site)
            if holding is not None and holding.workstation_id in invalid_ws:
                return True
        elif invalid_ws:
            if self._workstations_for(vreg, lowering).issubset(invalid_ws):
                return True
        if logistics_down and self._requires_transport(vreg, site, lowering):
            return True
        return False

    def _requires_transport(self, vreg: str, site: Optional[int], lowering: LoweringResult) -> bool:
        family = vreg.rsplit("_", 1)[0]
        source = next((location for name, location in self.f_t.state.wip_location.items()
                       if name.rsplit("_", 1)[0] == family), None)
        if not isinstance(site, int):
            candidates = lowering.eligible_sites.get(vreg, ())
            return all(self._requires_transport(vreg, candidate, lowering) for candidate in candidates)
        if source is None:
            return True
        return (self.f_t.holding_site(source).workstation_id !=
                self.f_t.holding_site(site).workstation_id)

    def _workstations_for(self, vreg: str, lowering: LoweringResult) -> Set[int]:
        workstations: Set[int] = set()
        for lowered in lowering.operations.values():
            instr = lowered.instr
            if not isinstance(instr, OpInstr):
                continue
            touched = list(instr.implicit_targets or [])
            if instr.target:
                touched.append(instr.target)
            touched.extend(instr.args)
            if vreg not in touched:
                continue
            workstations.update(lowered.candidate_workstations)
        return workstations

    def _expand(self, order_id: str, mdg, seeds: Set[str], allowed: Set[str]) -> Set[str]:
        if not seeds:
            return set()
        adjacency = self._adjacency(order_id, mdg)
        reached = {v for v in seeds if v in allowed}
        frontier = set(reached)
        for _ in range(self.max_hops):
            nxt: Set[str] = set()
            for vreg in frontier:
                for neighbour in adjacency.get(vreg, ()):
                    if neighbour in allowed and neighbour not in reached:
                        nxt.add(neighbour)
            if not nxt:
                break
            reached |= nxt
            frontier = nxt
        return reached

    def _adjacency(self, order_id: str, mdg) -> Dict[str, Set[str]]:
        if order_id in self._adjacency_cache:
            return self._adjacency_cache[order_id]
        adjacency: Dict[str, Set[str]] = {}
        if mdg is not None:
            for edge in mdg.edges:
                if edge.edge_type not in ("data", "phi", "exc"):
                    continue
                for a in self._defs_of(edge.src):
                    for b in self._defs_of(edge.dst) | self._uses_of(edge.dst):
                        if a == b:
                            continue
                        adjacency.setdefault(a, set()).add(b)
                        adjacency.setdefault(b, set()).add(a)
        self._adjacency_cache[order_id] = adjacency
        return adjacency

    @staticmethod
    def _defs_of(node) -> Set[str]:
        instr = node.instr
        if not isinstance(instr, OpInstr):
            return set()
        names = set(instr.implicit_targets or [])
        if instr.target:
            names.add(instr.target)
        return names

    @staticmethod
    def _uses_of(node) -> Set[str]:
        from compiler.target_lowering import material_arg_indices

        instr = node.instr
        if not isinstance(instr, OpInstr):
            return set()
        names = set()
        for idx in material_arg_indices(instr.op_type, len(instr.args)):
            if idx < len(instr.args):
                names.add(instr.args[idx])
        return names

    # ------------------------------------------------------------------
    # 3. 只重新落位受影响区域
    # ------------------------------------------------------------------
    def order_residual_versions(self, workset, order_id: str) -> Set[str]:
        """某订单尚未完成的物料版本（待定 + 携带）。"""
        if workset is None:
            return set()
        for fragment in workset.fragments:
            if fragment.order_id == order_id:
                return set(fragment.pending_vregs) | set(fragment.carried_vregs)
        return set()

    def recompile(
        self,
        region: AffectedRegion,
        workset: ResidualWorkset,
        allocation: Mapping[str, int],
        colorer: WorksetColorer,
        time: float,
    ) -> RecompileResult:
        if region.is_empty():
            return RecompileResult(region=region, rebuilt=False, reason="no_affected_material")

        fragments = [f for f in workset.fragments if region.by_order.get(f.order_id)]
        if not fragments:
            return RecompileResult(region=region, rebuilt=False, reason="no_fragment_matched")

        pinned = {
            vreg: site for vreg, site in allocation.items()
            if isinstance(site, int) and vreg not in region.expansion
        }
        color_result = colorer.color(
            workset.fragments,
            self.lowerings,
            pinned_locations=pinned,
            time=time,
            affected_vregs=region.by_order,
        )
        return RecompileResult(region=region, color_result=color_result, rebuilt=True,
                               reason="regional_rebinding")


# ----------------------------------------------------------------------
# 预编译异常处理
# ----------------------------------------------------------------------
@dataclass
class HandlerApplication:
    """一次预编译异常处理的应用结果。"""

    exception_type: str
    catch_block_id: Optional[int]
    start: float
    finish: float
    state: str
    delay: float
    region_vregs: Set[str] = field(default_factory=set)
    binding: Optional[object] = None

    def snapshot(self) -> Dict[str, object]:
        return {
            "exception_type": self.exception_type,
            "catch_block_id": self.catch_block_id,
            "start": self.start,
            "finish": self.finish,
            "state": self.state,
            "delay": self.delay,
            "handler_versions": len(self.region_vregs),
        }


class PrecompiledExceptionHandler:
    """
    U 事件的第一层处置：不重新规划，只按订单程序里预编译好的 catch 计划取出恢复步骤。

    catch 计划与异常表都是编译期产物；运行时只做"查表 + 把恢复动作平移到事件时刻"。
    """

    def __init__(self, cfg, exception_table: Sequence = (), catch_scenarios: Sequence = ()):
        self.cfg = cfg
        self.exception_table = list(exception_table or [])
        self.catch_scenarios = list(catch_scenarios or [])
        self._scene_by_exception: Dict[str, object] = {}
        for scenario in self.catch_scenarios:
            exc_type = self._exception_of_scenario(scenario)
            if exc_type and exc_type not in self._scene_by_exception:
                self._scene_by_exception[exc_type] = scenario

    def _exception_of_scenario(self, scenario):
        for entry in self.exception_table:
            if getattr(entry, "entry_id", None) == getattr(scenario, "scenario_id", None):
                return getattr(entry, "exception_type", None)
        trigger = getattr(scenario, "trigger_instr", None)
        if trigger is not None:
            for entry in self.exception_table:
                if getattr(entry, "trigger_instr", None) is trigger:
                    return getattr(entry, "exception_type", None)
        return getattr(scenario, "scenario_id", None)

    def handles(self, exception_type: Optional[str]) -> bool:
        if not exception_type:
            return False
        if any(getattr(entry, "exception_type", None) == exception_type
               for entry in self.exception_table):
            return True
        return exception_type in self._scene_by_exception

    def for_instruction(self, instr: OpInstr) -> "PrecompiledExceptionHandler":
        """异常表按触发程序点查找，其他工段的 catch 不覆盖当前指令。"""
        entries = [entry for entry in self.exception_table if entry.trigger_instr is instr]
        entry_ids = {entry.entry_id for entry in entries}
        scenarios = [scenario for scenario in self.catch_scenarios
                     if scenario.scenario_id in entry_ids]
        return PrecompiledExceptionHandler(self.cfg, entries, scenarios)

    def binding_for(self, event):
        entry = next(e for e in self.exception_table if e.exception_type == event.type.exception_type)
        kind = AVAIL_WORKSTATION if event.target_kind == TARGET_WORKSTATION else event.target_kind
        plans = self.cfg.precompiled_handler_bindings
        return plans.get((entry.entry_id, kind, event.target), plans[entry.entry_id, None, None])

    def exceptions_covered(self) -> Set[str]:
        return {getattr(entry, "exception_type", "") for entry in self.exception_table} - {""}

    def recovery_instructions(self, exception_type: str, catch_block_id=None):
        """恢复动作包含 catch 被拆分后的全部续块，同一块只读取一次。"""
        instructions = []
        seen = set()
        for entry in self.exception_table:
            if entry.exception_type != exception_type:
                continue
            block = entry.catch_block
            if catch_block_id is not None and block.id != catch_block_id:
                continue
            prefix = block.name
            pending = [block]
            while pending:
                current = pending.pop(0)
                if current.id in seen:
                    continue
                seen.add(current.id)
                instructions.extend(i for i in current.instructions if isinstance(i, OpInstr))
                pending.extend(b for b in current.successors if b.name.startswith(prefix))
        return instructions

    def handler_vregs(self, exception_type: str) -> Set[str]:
        """
        处理器产生的物料版本。

        这就是 FaLang 版的"handler live-in"：处理器被激活后，这些版本进入残余工作集，
        与其它残余物料走同一套图着色。
        """
        vregs: Set[str] = set()
        for instr in self.recovery_instructions(exception_type):
            vregs.update(instr.implicit_targets or [])
            if instr.target:
                vregs.add(instr.target)
            vregs.update(arg for arg in instr.args if isinstance(arg, str))
        return vregs

    def apply(self, exception_type: str, t_exc: float) -> Optional[HandlerApplication]:
        """
        取出该异常类型对应的预编译恢复步骤，并把它平移到本次事件发生的时刻。

        编译期给出的 contingency 时刻以"触发指令时刻"为原点，运行时改由实际事件时刻决定起点，
        因此这里只取恢复动作的相对时长，再叠加到 t_exc 上。
        """
        scenario = self._scene_by_exception.get(exception_type)
        if scenario is None:
            return None

        base = float(getattr(scenario, "t_exc", 0.0) or 0.0)
        catch_block_id = getattr(scenario, "catch_block_id", None)
        duration = 0.0
        for instr in self.recovery_instructions(exception_type, catch_block_id):
            contingency_finish = getattr(instr, "contingency_finish", None)
            if contingency_finish is not None:
                duration = max(duration, float(contingency_finish) - base)
        duration = max(0.0, duration)

        start = float(t_exc)
        return HandlerApplication(
            exception_type=exception_type,
            catch_block_id=catch_block_id,
            start=start,
            finish=start + duration,
            state=str(getattr(scenario, "state", "scheduled")),
            delay=duration,
            region_vregs=self.handler_vregs(exception_type),
        )


# ----------------------------------------------------------------------
# 协调：事件 → 属性 → 策略 → 处置
# ----------------------------------------------------------------------
@dataclass
class HandlingOutcome:
    """一次不确定性事件被处理之后的完整记录。"""

    event: UncertaintyEvent
    attributes: EventAttributes
    plan: HandlingPlan
    handler_application: Optional[HandlerApplication] = None
    # 处理器的物料未能落位（触发时刻拿不到资源）：处置动作起不来，只能等
    handler_material_waiting: Tuple[str, ...] = ()
    recompilation_time_s: float = 0.0
    region: Optional[AffectedRegion] = None
    recompile: Optional[RecompileResult] = None
    rebound: Dict[str, int] = field(default_factory=dict)
    still_waiting: List[str] = field(default_factory=list)

    def snapshot(self) -> Dict[str, object]:
        return {
            "event": self.event.snapshot(),
            "attributes": self.attributes.snapshot(),
            "plan": self.plan.snapshot(),
            "handler": self.handler_application.snapshot() if self.handler_application else None,
            "region": self.region.snapshot() if self.region else None,
            "recompile": self.recompile.snapshot() if self.recompile else None,
            "rebound": len(self.rebound),
            "still_waiting": len(self.still_waiting),
        }


class UncertaintyCoordinator:
    """把"事件 → 属性 → 策略 → 处置 → WAIT/retry"串起来。"""

    def __init__(
        self,
        f_t: FactoryTargetMachine,
        lowerings: Mapping[str, LoweringResult],
        workset: ResidualWorkset,
        colorer: WorksetColorer,
        policy: ResponsePolicy = ResponsePolicy.HYBRID,
        handlers: Optional[Mapping[str, PrecompiledExceptionHandler]] = None,
        max_hops: int = 0,
    ):
        self.f_t = f_t
        self.workset = workset
        self.colorer = colorer
        self.policy = policy
        # 区域边界规则。containment 是本文的方法；其余为文献通行的启发式基线。
        #  · whole_workset —— 重排系统内在制的一切（边界过大的一端）
        #  · machine_group —— 按故障设备切分（设备组式边界）
        #  · event_order   —— 只重排事件直接命中的那张工单（边界过小的一端，
        #                     schedule repair / DERS 一类，Rahmani & Ramezanian 2025 的 DERS）
        self.region_rule = "containment"
        self.handlers = dict(handlers or {})
        self.detector = RegionalResidualRecompilation(f_t, dict(lowerings), max_hops=max_hops)
        self._lowerings: Dict[str, LoweringResult] = dict(lowerings)
        self.allocation: Dict[str, int] = {}
        self.waiting: Set[str] = set()
        self.history: List[HandlingOutcome] = []

    # ------------------------------------------------------------------
    @property
    def lowerings(self) -> Dict[str, LoweringResult]:
        return self._lowerings

    @lowerings.setter
    def lowerings(self, value: Mapping[str, LoweringResult]) -> None:
        """
        更新降级结果时必须**同时**同步给 detector。

        受影响区域判定、候选集作废判定都走 detector，而 detector 持有自己的一份
        lowerings。只改协调器这一份，detector 会一直用构造时那份（通常是空字典），
        于是所有"谁的候选集被作废了"的判定都返回空——containment 恒为真，
        区域性重编译也永远检不出区域。
        """
        self._lowerings = dict(value)
        detector = getattr(self, "detector", None)
        if detector is not None:
            detector.lowerings = self._lowerings

    def set_allocation(self, allocation: Mapping[str, int]) -> None:
        self.allocation = {v: s for v, s in allocation.items() if isinstance(s, int)}

    def handler_for(self, event: UncertaintyEvent) -> Optional[PrecompiledExceptionHandler]:
        if event.order_id and event.order_id in self.handlers:
            return self.handlers[event.order_id]
        return next(iter(self.handlers.values()), None)

    # ------------------------------------------------------------------
    def handle(self, event: UncertaintyEvent,
               allocation: Optional[Mapping[str, int]] = None) -> HandlingOutcome:
        if allocation is not None:
            self.set_allocation(allocation)
        current = dict(self.allocation)

        # 先把事件对 F_t 的影响写进去，**再**判定属性。
        # containment 问的是"事件已经发生、资源已经不可用之后，处理器链条之外的
        # 版本还能不能落位"；若在写 F_t 之前判定，被作废的工位此刻看着还是好的，
        # 判据恒为"兜得住"，预定义策略也就永远不会暴露它兜不住的那一面。
        if event.type.invalidates_resource:
            self.detector.apply_state_change(event)

        attributes = self.attributes_of(event, current)
        plan = self.policy.plan(attributes)
        outcome = HandlingOutcome(event=event, attributes=attributes, plan=plan)

        if plan.use_handler:
            outcome.handler_application = self._activate_handler(event)
            # 激活首次编译的固定资源方案，运行时不重新求解 handler 的绑定。
            self._bind_handler_material(event, outcome.handler_application)
            current = dict(self.allocation)
            if outcome.handler_application is not None:
                region = set(outcome.handler_application.region_vregs)
                binding = outcome.handler_application.binding
                outcome.handler_material_waiting = tuple(sorted(
                    v for v in region & set(binding.allocation)
                    if not self._fixed_site_available(v, binding.allocation[v], event.time)))

        if plan.recompile:
            seeds = None
            if plan.recompile_scope == "uncovered_part":
                seeds = self.detector.versions_with_invalidated_candidates(
                    event, self.workset, event.time,
                    exclude=attributes_handler_vregs(outcome.handler_application),
                    allocation=current,
                )
            elif not event.type.invalidates_resource and event.order_id:
                # 缺料、质量偏差、工装偏位这类事件不会作废任何资源，因此按资源反查
                # 种子永远查不到东西，区域会被判成空、"重编译"变成空转。
                # 它们的受影响区域就是**事件落在的那张订单尚未完成的全部物料版本**：
                # 这些活的落位需要重做。
                seeds = self.detector.order_residual_versions(self.workset, event.order_id)
            if self.region_rule != "containment":
                seeds = self._baseline_boundary(event, seeds)
            region = self.detector.detect_region(
                event, self.workset, current, event.time,
                seeds=seeds,
            )
            outcome.region = region
            compile_started = perf_counter()
            outcome.recompile = self.detector.recompile(
                region, self.workset, current, self.colorer, event.time
            )
            if outcome.recompile.rebuilt:
                outcome.recompilation_time_s = perf_counter() - compile_started
            if outcome.recompile.color_result is not None:
                self._absorb(outcome.recompile.color_result)

        outcome.still_waiting = sorted(self.waiting)
        self.history.append(outcome)
        return outcome

    # ------------------------------------------------------------------
    def _baseline_boundary(self, event: UncertaintyEvent, seeds):
        """
        文献通行的启发式区域边界。三者都只改"圈多大"，不改"圈住之后怎么重编译"。
        containment 判据要回答的正是：这些按时间/设备/工单切出来的边界，
        是不是把该重排的都圈住了、又有没有多圈。
        """
        fragments = list(self.workset.fragments) if self.workset else []
        if self.region_rule == "event_order":
            # 只圈事件直接命中的那张工单（SDR/DERS 式的 schedule repair）
            return self.detector.order_residual_versions(self.workset, event.order_id)
        if self.region_rule == "machine_group":
            # 库位属于其所在工位；共享物流故障覆盖所有工位；物料事件沿用订单区域。
            if event.target_kind == TARGET_WORKSTATION:
                stations = {event.target}
            elif event.target_kind == TARGET_HOLDING:
                stations = {self.f_t.holding_site(event.target).workstation_id}
            elif event.target_kind == TARGET_LOGISTICS:
                return set(self.workset.pending_vregs) | set(self.workset.carried_vregs)
            else:
                return self.detector.order_residual_versions(self.workset, event.order_id)
            picked = set()
            for fragment in fragments:
                lowering = self.lowerings.get(fragment.order_id)
                if lowering is None:
                    continue
                for vreg in set(fragment.pending_vregs) | set(fragment.carried_vregs):
                    workstations = {
                        self.f_t.holding_site(site).workstation_id
                        for site in (lowering.eligible_sites.get(vreg) or [])
                        if self.f_t.holding_site(site) is not None
                    }
                    if stations & workstations:
                        picked.add(vreg)
            return picked
        if self.region_rule == "whole_workset":
            # 边界过大的一端：把系统里当前在制的一切都重排。
            # 这正是文献所说"边界太大同时抬高复杂度与 nervousness"的形态。
            picked = set()
            for fragment in fragments:
                picked |= set(fragment.pending_vregs) | set(fragment.carried_vregs)
            return picked
        return seeds

    def _bind_handler_material(self, event: UncertaintyEvent,
                                application: Optional[HandlerApplication]) -> None:
        """直接安装首次编译的固定绑定，不调用着色或挑选替代资源。"""
        if application is None or not application.region_vregs:
            return
        order_id = event.order_id
        if not order_id:
            return
        fragment = next((f for f in self.workset.fragments if f.order_id == order_id), None)
        if fragment is None:
            return
        targets = application.region_vregs & set(fragment.pending_vregs)
        for vreg in targets & set(application.binding.allocation):
            self.allocation[vreg] = application.binding.allocation[vreg]
            self.waiting.discard(vreg)

    def _fixed_site_available(self, vreg, site, time):
        source = next((p for v,p in self.f_t.state.wip_location.items()
                       if v.rsplit('_',1)[0] == vreg.rsplit('_',1)[0]), None)
        own = int(source == site)
        return (self.f_t.is_site_available(site, time, source_site=source) and
                self.f_t.capacity_of(site) - self.f_t.occupancy_of(site) + own >= 1)

    def attributes_of(self, event: UncertaintyEvent,
                      allocation: Optional[Mapping[str, int]] = None) -> EventAttributes:
        """判定事件在上下文中的三个属性。"""
        current = dict(self.allocation if allocation is None else allocation)
        handler = self.handler_for(event)
        exception_type = event.type.exception_type
        anticipated = bool(handler is not None and handler.handles(exception_type))

        attributes = EventAttributes(anticipated=anticipated)
        if not anticipated:
            # 没有预定义处理器时，只判断事件是否作废了别的东西
            attributes.contained = not event.type.invalidates_resource or not (
                self.detector.versions_with_invalidated_candidates(
                    event, self.workset, event.time, allocation=current)
            )
            attributes.uncovered_versions = len(
                self.detector.versions_with_invalidated_candidates(
                    event, self.workset, event.time, allocation=current)
            )
            return attributes

        attributes.handler_exception_type = exception_type
        application = self._activate_handler(event)
        handler_vregs = application.region_vregs if application else set()
        attributes.handler_region = f"catch_for_{exception_type}"

        # executability at trigger：处理器首次编译的固定资源方案此刻是否可执行
        attributes.executable_at_trigger = self._handler_executable(event)

        if not event.type.invalidates_resource:
            attributes.contained = True
            return attributes

        uncovered = self.detector.versions_with_invalidated_candidates(
            event, self.workset, event.time, exclude=handler_vregs, allocation=current
        )
        attributes.uncovered_versions = len(uncovered)
        attributes.contained = not uncovered
        return attributes

    def _handler_executable(self, event: UncertaintyEvent) -> bool:
        """处理器的每条恢复动作均须有可用资源，包括 catch 续块。"""
        handler = self.handler_for(event)
        if handler is None:
            return False
        binding = handler.binding_for(event)
        for instr in handler.recovery_instructions(event.type.exception_type):
            lowered = next((lowering.operations[id(instr)] for lowering in self.lowerings.values()
                            if id(instr) in lowering.operations), None)
            if lowered is None:
                return False
            if not lowered.is_physical:
                continue
            if lowered.is_logistics:
                logistics = self.f_t.logistics_resource()
                if not self.f_t.is_available(AVAIL_LOGISTICS, logistics.palu_id, event.time):
                    return False
            elif not self.f_t.is_available(AVAIL_WORKSTATION, binding.actions[id(instr)][1], event.time):
                return False
        return all(self._fixed_site_available(v,p,event.time) for v,p in binding.allocation.items())

    def _activate_handler(self, event: UncertaintyEvent) -> Optional[HandlerApplication]:
        handler = self.handler_for(event)
        if handler is None:
            return None
        application = handler.apply(event.type.exception_type, event.time)
        if application is not None:
            application.binding = handler.binding_for(event)
        if application is not None and event.op_key:
            for fragment in self.workset.fragments:
                if fragment.order_id != event.order_id:
                    continue
                for block in fragment.cfg.blocks:
                    for instr in block.instructions:
                        if (not isinstance(instr, OpInstr) or
                                fragment.instr_op.get(id(instr)) != event.op_key):
                            continue
                        definitions = set(instr.implicit_targets or ())
                        if instr.target:
                            definitions.add(instr.target)
                        application.region_vregs.update(definitions & fragment.pending_vregs)
        return application

    # ------------------------------------------------------------------
    def release(self, event: UncertaintyEvent, allocation: Optional[Mapping[str, int]] = None):
        """资源恢复：撤销不可用标记，再对仍在等待的版本重试。"""
        released = self.detector.release_state_change(event)
        if allocation is not None:
            self.set_allocation(allocation)
        retry = None
        if self.waiting:
            current = dict(self.allocation)
            fragments = [f for f in self.workset.fragments if f.pending_vregs & self.waiting]
            if fragments:
                retry = self.colorer.color(
                    self.workset.fragments,
                    self.lowerings,
                    pinned_locations={
                        vreg: site for vreg, site in current.items()
                        if vreg not in self.waiting
                    },
                    time=event.time,
                    affected_vregs={
                        f.order_id: (f.pending_vregs & self.waiting) for f in fragments
                    },
                )
                self._absorb(retry)
        return released, retry

    # ------------------------------------------------------------------
    def _absorb(self, color_result: WorksetColorResult) -> None:
        merged = dict(self.allocation)
        for vreg in color_result.waiting:
            merged.pop(vreg, None)
        merged.update(color_result.allocation)
        self.allocation = merged
        self.waiting -= set(color_result.allocation)
        self.waiting |= set(color_result.waiting)

    def snapshot(self) -> Dict[str, object]:
        return {
            "policy": self.policy.value,
            "events": len(self.history),
            "waiting": len(self.waiting),
            "assigned": len(self.allocation),
        }


def attributes_handler_vregs(application: Optional[HandlerApplication]) -> Set[str]:
    return set(application.region_vregs) if application else set()
