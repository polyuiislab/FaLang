# F_IDE_Project/compiler/residual_coloring.py
"""
Paper 1 Method 2 —— Residual Graph Coloring：面向制造可行性的物料持有位置分配。

制造类比：物料在被生产出来的那一刻起，就"占住"了某个暂存位置，直到被下一道工序取走。
因此可以把**物料版本**看作图的一个顶点，把**占位时间重叠**看作冲突边，把**可行的暂存位置**
看作颜色：同一条边上两个顶点不能占同一个位置。

    vertex  = 物料/WIP 的 SSA 版本
    lifetime = SSA 定义/使用 + MDG 的 ASAP 传播（不依赖任何 Pre-RA 调度）
    edge    = 两个版本的占位时间重叠
    color   = 满足可行性条件的物理持有位置

与 Paper 2 的 PBQP 分配不同，这里**只判可行、不比较代价**：

    候选位置(ρ)  <=>  生产该版本的工序能在 w(ρ) 执行
                  AND  C^free_ρ(t) = C_ρ - O_ρ(t) - R_ρ(t) >= 1
                  AND  ρ 与其所在工位当前 available

当一个版本当前完全没有候选位置时，它进入 **WAIT**，而不是被判定为不可行、被塞进
warehouse、或者被换成一条人工插入的存取指令。等到有位置释放（工序取走物料、设备或
库位恢复），再对 WAIT 中的版本及其残余干涉邻域重新着色。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from compiler.ir import OpInstr, PhiInstr, BasicBlock, ControlFlowGraph
from compiler.register_alloc import RegisterAllocator
from compiler.factory_target_machine import FactoryTargetMachine, FactoryState
from compiler.target_lowering import LoweredOperation, LoweringResult


@dataclass
class ColorRoundResult:
    """一轮 Residual Graph Coloring 的结果。"""

    time: float
    colored: Dict[str, int] = field(default_factory=dict)
    waiting: List[str] = field(default_factory=list)
    # WAIT 的版本 -> 它当前缺容量的候选位置（用于定位"等的是哪个资源"）
    blocked_on: Dict[str, List[int]] = field(default_factory=dict)
    # 本轮完全可行但因无候选而未能着色的版本数
    unavailable_reason: Dict[str, str] = field(default_factory=dict)


@dataclass
class ColoringOutcome:
    """一次完整分配（可能包含多轮 WAIT/retry）的结果。"""

    allocation: Dict[str, int] = field(default_factory=dict)
    waiting: List[str] = field(default_factory=list)
    rounds: List[ColorRoundResult] = field(default_factory=list)
    wait_events: int = 0

    @property
    def wait_rounds(self) -> int:
        return sum(1 for r in self.rounds if r.waiting)

    def snapshot(self) -> Dict[str, object]:
        return {
            "assigned": len(self.allocation),
            "waiting": len(self.waiting),
            "rounds": len(self.rounds),
            "wait_events": self.wait_events,
        }


# ----------------------------------------------------------------------
# 落位选择器：从**合格**库位里挑一个（Study 3）
# ----------------------------------------------------------------------
# 合格性判定（能力匹配、区间容量、可用性）由 `_candidate_sites` 统一负责，四个
# 选择器完全共用；选择器只决定"从合格库里挑哪一个"。这样方法之间的差异只来自
# 选择策略本身，不会混入判据差异。
#
# 两条排序键构成 2×2 析因：
#   transport —— 衔接输入物料与下一道工序，合并相邻版本的工位
#   used     —— 是否看库位占用（负载 / 干涉）
class SiteSelector:
    """落位选择器基类。"""

    name = "base"
    description = ""

    def sort_key(self, vreg: str, site_id: int, colorer):
        raise NotImplementedError


class GraphColoringSelector(SiteSelector):
    """本研究方法：**联合**考虑搬运预判与库位占用。"""

    name = "graph_coloring"
    description = "transport anticipation + load (residual graph coloring)"

    def sort_key(self, vreg, site_id, colorer):
        site = colorer.f_t.holding_site(site_id)
        return (colorer._transfer_count(vreg, site_id),
                colorer._site_load(site_id, vreg),
                site.workstation_id, site_id)


class FirstFitSelector(SiteSelector):
    """经典首次适配：取第一个合格库位。既不看搬运，也不看负载。"""

    name = "first_fit"
    description = "first eligible site (capacity only)"

    def sort_key(self, vreg, site_id, colorer):
        return (site_id,)


class TransportJointSelector(SiteSelector):
    """机器—运输联合一类：预判搬运，但不看库位占用。"""

    name = "machine_transport_joint"
    description = "transport anticipation only, no load awareness"

    def sort_key(self, vreg, site_id, colorer):
        site = colorer.f_t.holding_site(site_id)
        return (colorer._transfer_count(vreg, site_id),
                site.workstation_id, site_id)


class LoadBalanceSelector(SiteSelector):
    """生产—物流协同 / 负载均衡一类：看库位占用，但不预判搬运。"""

    name = "load_balance"
    description = "load balance only, no transport anticipation"

    def sort_key(self, vreg, site_id, colorer):
        site = colorer.f_t.holding_site(site_id)
        return (colorer._site_load(site_id, vreg),
                site.workstation_id, site_id)


SITE_SELECTORS = {
    cls.name: cls for cls in
    (GraphColoringSelector, FirstFitSelector, TransportJointSelector, LoadBalanceSelector)
}


def make_site_selector(name: str) -> "SiteSelector":
    if name not in SITE_SELECTORS:
        raise KeyError(f"unknown site selector {name!r}; known: {sorted(SITE_SELECTORS)}")
    return SITE_SELECTORS[name]()


class ResidualGraphColorer(RegisterAllocator):
    """
    残余图着色器。

    复用 RegisterAllocator 的顶点收集、定值/使用分析与 PALU 绑定；替换掉其分配部分：
    不用 PBQP、不插 spill、不写 memory、不做 Post-RA 相关的修复。
    """

    def __init__(
        self,
        cfg: ControlFlowGraph,
        mdg,
        f_t: FactoryTargetMachine,
        lowering: LoweringResult,
        state: Optional[FactoryState] = None,
        selector: Optional["SiteSelector"] = None,
        residual_vregs: Optional[Iterable[str]] = None,
        pinned_locations: Optional[Mapping[str, int]] = None,
        manage_reservations: bool = True,
        handler_vregs: Optional[Iterable[str]] = None,
        emit_moves: bool = True,
    ):
        self.f_t = f_t if state is None else f_t.with_state(state)
        self.emit_moves = emit_moves
        self.selector = selector or GraphColoringSelector()
        self._covering_cache: Dict[str, Set[int]] = {}
        self.lowering = lowering
        # 由 Workset Colorer 统筹多个订单时，预留账本由上层维护，避免互相清空
        self.manage_reservations = manage_reservations
        # 携带类版本：位置由运行时状态给定，只能沿用
        self.pinned: Dict[str, int] = dict(pinned_locations or {})
        self._physical_sites = {name.rsplit("_", 1)[0]: site
                                for name, site in self.f_t.state.wip_location.items()}
        # schedule_result=None ⇒ 生命期改由 MDG 的 ASAP 传播给出
        super().__init__(cfg, mdg, None, f_t._tm)
        if residual_vregs is not None:
            # 只对残余工作集内的版本重新落位；冻结/携带版本不进入着色。
            # 被激活的处理器物料必须留下，它们同时也是本次要落位的对象。
            allowed = set(residual_vregs) | set(handler_vregs or ())
            self.vregs = [v for v in self.vregs if v in allowed]
        if handler_vregs:
            # 预编译处理器被激活时，它的物料版本进入残余工作集，与其它残余物料一样着色。
            # 这对应 Java 编译器"handler 的 live-in 也要分配"的做法：处理器不是一段
            # 事后补绑的附加代码，而是一条有异常入口活性集的普通代码路径。
            self._include_handler_vregs(set(handler_vregs))

        self.waiting: Set[str] = set()
        self._colored: Set[str] = set()
        # 已经被下游工序取走的版本：它们已完成落位，不再参与任何一轮决策
        self._consumed: Set[str] = set()
        self._finalized = False
        self.rounds: List[ColorRoundResult] = []
        # 携带类版本直接占用其既成位置，作为本轮的既有占用
        for vreg, site_id in self.pinned.items():
            self.allocation[vreg] = site_id
            self._colored.add(vreg)
            if self.manage_reservations and self._carried_site(vreg) != site_id:
                start, end = self._holding_span(vreg)
                self.f_t.reserve_interval(site_id, start, end)

    # ------------------------------------------------------------------
    # 覆写：Paper 1 不使用 Pre-RA 调度，也不使用异常路径等宽生命期
    # ------------------------------------------------------------------
    def _build_instr_time_map(self) -> None:
        # 生命期完全由 SSA + MDG 推导，不接受调度器的时刻表。
        self.instr_times: Dict[int, Tuple[float, float]] = dict(getattr(self, "instr_times", {}) or {})

    def _include_handler_vregs(self, handler_vregs: Set[str]) -> None:
        """
        把异常路径上的物料版本纳入本次分配。

        这些版本只在处理器被激活时才存在，因此它们的定义/使用都在 catch / compensate 块里，
        而 `_collect_vregs` / `_analyze_uses` 默认跳过异常路径块。
        """
        present = set(self.vregs)
        added: Set[str] = set()
        for block in self.cfg.blocks:
            if not self._is_exception_path_block(block):
                continue
            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue
                defined = []
                if instr.target and self._is_vreg(instr.target):
                    defined.append(instr.target)
                for extra in instr.implicit_targets or []:
                    if self._is_vreg(extra):
                        defined.append(extra)
                for name in defined:
                    if name not in handler_vregs:
                        continue
                    added.add(name)
                    self.def_instrs[name] = (block.id, idx, instr)
                for pos in self._material_arg_indices(instr.op_type, len(instr.args)):
                    if pos >= len(instr.args):
                        continue
                    name = instr.args[pos]
                    if not self._is_vreg(name) or name not in handler_vregs:
                        continue
                    added.add(name)
                    self.use_instrs.setdefault(name, []).append((block.id, idx, instr))

        new_names = sorted(added - present)
        if not new_names:
            return
        self.vregs = sorted(present | added)
        self._compute_handler_liveness(added)

    def _compute_handler_liveness(self, handler_vregs: Set[str]) -> None:
        """处理器物料的占位区间：从抛出点起，到处理器最后一步用完为止。"""
        from compiler.mdg import MDGNode  # noqa: F401  （仅用于类型一致性）

        asap: Dict[int, float] = {}
        for node in self.mdg.nodes:
            if node.node_type != "op" or not isinstance(node.instr, OpInstr):
                continue
            start = 0.0
            for edge in node.in_edges:
                if edge.edge_type in ("data", "struct") and edge.src.id in asap:
                    start = max(start, asap[edge.src.id] + edge.weight)
            asap[node.id] = start

        def latency(node) -> float:
            if not isinstance(node.instr, OpInstr) or node.instr.expected_time is None:
                return 0.0
            return float(node.instr.expected_time)

        span_by_vreg: Dict[str, Tuple[float, float]] = {}
        for node in self.mdg.nodes:
            if node.node_type != "op" or not isinstance(node.instr, OpInstr):
                continue
            instr = node.instr
            names = set(instr.implicit_targets or [])
            if instr.target:
                names.add(instr.target)
            names &= handler_vregs
            if names:
                start = asap.get(node.id, 0.0)
                end = start + latency(node)
                for name in names:
                    prev = span_by_vreg.get(name)
                    span_by_vreg[name] = (
                        min(prev[0], start) if prev else start,
                        max(prev[1], end) if prev else end,
                    )

        fallback_end = max(asap.values(), default=0.0) + 1.0
        for vreg in handler_vregs:
            start, end = span_by_vreg.get(vreg, (0.0, fallback_end))
            self.vreg_liveness[vreg] = (start, max(end, start + 1e-9))

    def _compute_exception_liveness(self) -> Dict[str, Tuple[float, float]]:
        # catch/compensate 路径的绑定由 catch 补偿延迟规划负责，不进入着色。
        return {}

    def _compute_liveness_from_mdg(self) -> None:
        """基于 MDG 的 ASAP 传播计算占位区间，不依赖任何调度结果。"""
        from collections import deque

        op_nodes = [n for n in self.mdg.nodes if n.node_type == "op"]
        if not op_nodes:
            return
        by_id: Dict[int, object] = {n.id: n for n in op_nodes}
        nids = set(by_id)

        in_deg: Dict[int, int] = {n.id: 0 for n in op_nodes}
        adj: Dict[int, List[object]] = {n.id: [] for n in op_nodes}
        for node in op_nodes:
            for edge in node.in_edges:
                if edge.edge_type in ("data", "struct") and edge.src.id in nids:
                    in_deg[node.id] += 1
            for edge in node.out_edges:
                if edge.edge_type in ("data", "struct") and edge.dst.id in nids:
                    adj[node.id].append(edge)

        queue = deque(nid for nid, deg in in_deg.items() if deg == 0)
        order: List[int] = []
        while queue:
            nid = queue.popleft()
            order.append(nid)
            for edge in adj.get(nid, []):
                in_deg[edge.dst.id] -= 1
                if in_deg[edge.dst.id] == 0:
                    queue.append(edge.dst.id)
        if not order:
            return

        asap: Dict[int, float] = {}
        for nid in order:
            node = by_id[nid]
            start = 0.0
            for edge in node.in_edges:
                if edge.edge_type in ("data", "struct") and edge.src.id in asap:
                    start = max(start, asap[edge.src.id] + edge.weight)
            asap[nid] = start

        def latency(node) -> float:
            if not isinstance(node.instr, OpInstr) or node.instr.expected_time is None:
                return 0.0
            return float(node.instr.expected_time)

        makespan = 0.0
        for nid, start in asap.items():
            makespan = max(makespan, start + latency(by_id[nid]))

        def_nodes: Dict[str, object] = {}
        use_nodes: Dict[str, List[object]] = {}
        for node in op_nodes:
            instr = node.instr
            if not isinstance(instr, OpInstr):
                continue
            if instr.target and self._is_vreg(instr.target):
                def_nodes[instr.target] = node
            for extra in instr.implicit_targets or []:
                if self._is_vreg(extra):
                    def_nodes[extra] = node
            for idx in self._material_arg_indices(instr.op_type, len(instr.args)):
                if idx < len(instr.args) and self._is_vreg(instr.args[idx]):
                    use_nodes.setdefault(instr.args[idx], []).append(node)

        for vreg in self.vregs:
            def_node = def_nodes.get(vreg)
            uses = use_nodes.get(vreg, [])
            start = asap.get(def_node.id, 0.0) if def_node is not None else 0.0
            if uses:
                end = max(asap.get(node.id, 0.0) + latency(node) for node in uses)
            elif def_node is not None:
                end = asap.get(def_node.id, 0.0) + latency(def_node)
            else:
                end = makespan
            self.vreg_liveness[vreg] = (start, max(end, start))

        self.instr_times = {}
        for node in op_nodes:
            if node.id in asap and node.instr is not None:
                self.instr_times[id(node.instr)] = (asap[node.id], asap[node.id] + latency(node))

    # ------------------------------------------------------------------
    # 干涉图
    # ------------------------------------------------------------------
    def _build_interference(self) -> Dict[str, Set[str]]:
        """占位区间重叠 ⇒ 冲突。用扫描线构造，避免 O(V^2) 全对比较。"""
        graph: Dict[str, Set[str]] = {v: set() for v in self.vregs}
        ordered = sorted(
            self.vregs,
            key=lambda v: self.vreg_liveness.get(v, (0.0, 0.0))[0],
        )
        active: List[str] = []
        for vreg in ordered:
            start, _ = self.vreg_liveness.get(vreg, (0.0, 0.0))
            still_active = []
            for other in active:
                _, other_end = self.vreg_liveness.get(other, (0.0, 0.0))
                if other_end > start:
                    still_active.append(other)
                    graph[vreg].add(other)
                    graph[other].add(vreg)
            active = still_active
            active.append(vreg)

        # 线性物料约束：同一条指令不能同时持有它消费/产出的两个版本
        by_instr: Dict[int, List[str]] = {}
        for vreg in self.vregs:
            for site in (self.def_instrs.get(vreg),):
                if site:
                    by_instr.setdefault(id(site[2]), []).append(vreg)
            for bid, idx, instr in self.use_instrs.get(vreg, []):
                by_instr.setdefault(id(instr), []).append(vreg)
        for _, vregs in by_instr.items():
            for i, vi in enumerate(vregs):
                for vj in vregs[i + 1:]:
                    if vi != vj:
                        graph[vi].add(vj)
                        graph[vj].add(vi)
        return graph

    # ------------------------------------------------------------------
    # 候选颜色
    # ------------------------------------------------------------------
    def _eligible_sites(self, vreg: str) -> List[int]:
        """Target Lowering 已经算出的、满足生产工序能力约束的物理持有位置。"""
        sites = self.lowering.eligible_sites.get(vreg)
        if sites is not None:
            return list(sites)
        return sorted(site.storage_id for site in self.f_t.holding_sites())

    def _candidate_sites(self, vreg: str) -> Tuple[List[int], List[int], str]:
        """
        返回 (可用于着色的位置, 被挡下的位置, 原因)。

        只看三条硬条件：能力相容、占位区间内始终放得下、当前可用。没有任何代价或惩罚项。

        容量按**时间区间**判定（C^free_p(t) ≥ 1 在整个占位区间内成立），
        而不是按"这个位置有没有被别人用过"。前者允许先后复用同一库位，
        也允许容量 ≥ 2 的库位同时放两件互相冲突的物料；后者会把容量 2 当成容量 1 用。
        """
        eligible = self._eligible_sites(vreg)
        if not eligible:
            return [], [], "no_capability_compatible_site"

        start, end = self.vreg_liveness.get(vreg, (0.0, 0.0))
        if end <= start:
            end = start + 1e-9

        blocked: List[int] = []
        usable: List[int] = []
        for site_id in eligible:
            if self.f_t.holding_site(site_id) is None:
                continue
            if not self.f_t.is_site_available(site_id, source_site=self._carried_site(vreg)):
                blocked.append(site_id)
                continue
            # 同一件物料在原位加工只更新版本，不新增一份实物占用。
            own = int(self._carried_site(vreg) == site_id)
            if self.f_t.free_capacity_over(site_id, start, end) + own < 1:
                blocked.append(site_id)
                continue
            usable.append(site_id)
        if not usable and blocked:
            return [], blocked, "no_free_capacity_or_unavailable"
        return usable, blocked, ""

    def _order_key(self, vreg: str, interference: Dict[str, Set[str]]):
        start, end = self.vreg_liveness.get(vreg, (0.0, 0.0))
        return (-len(interference.get(vreg, set())), -(end - start), start, vreg)

    def _carried_site(self, vreg: str) -> Optional[int]:
        return self._physical_sites.get(vreg.rsplit("_", 1)[0])

    def _site_load(self, site_id: int, vreg: str) -> int:
        """该生命期内的占用峰值，包含其他订单已经登记的区间预留。"""
        start, end = self._holding_span(vreg)
        return self.f_t.capacity_of(site_id) - self.f_t.free_capacity_over(site_id, start, end)

    def _covering_sites(self, vreg: str) -> Set[int]:
        """所在工位也能执行该版本下游消费工序的库位——落在这些位置可少一次跨工位搬运。"""
        cached = self._covering_cache.get(vreg)
        if cached is not None:
            return cached
        covering: Set[int] = set()
        for cap in self.lowering.consuming_capabilities.get(vreg) or set():
            if self.f_t.is_logistics_capability(cap):
                continue
            for ws_id in self.f_t.workstations_supporting(cap):
                covering.update(self.f_t.holding_sites_of_workstation(ws_id))
        self._covering_cache[vreg] = covering
        return covering

    def _transfer_count(self, vreg: str, site_id: int) -> int:
        """当前输入与下一工序所需的跨工位搬运次数；相邻版本原位交接为零。"""
        source = self._carried_site(vreg)
        definition = self.def_instrs.get(vreg)
        if definition is not None:
            instr = definition[2]
            for pos in self._material_arg_indices(instr.op_type, len(instr.args)):
                planned = self.allocation.get(instr.args[pos])
                if isinstance(planned, int):
                    source = planned
                    break
        incoming = int(source is not None and
                       self.f_t.holding_site(source).workstation_id !=
                       self.f_t.holding_site(site_id).workstation_id)
        covering = self._covering_sites(vreg)
        return incoming + int(bool(covering) and site_id not in covering)

    def _pick_site(self, vreg: str, usable: Sequence[int]) -> int:
        """
        在可行位置中做出确定性选择——委托给 `SiteSelector`。

        合格性（能力/区间容量/可用性）由 `_candidate_sites` 统一判定，四个选择器共用；
        选择器只决定"从合格库里挑哪一个"。
        """
        chosen = min(usable, key=lambda site_id: self.selector.sort_key(vreg, site_id, self))
        station = self.f_t.holding_site(chosen).workstation_id
        family = vreg.rsplit("_", 1)[0]
        # 同工位的连续版本表示同一件实物，无需在两处线边库位之间互换。
        related = [self._carried_site(vreg)] + [
            site for name, site in self.allocation.items() if name.rsplit("_", 1)[0] == family]
        return next((site for site in related if site in usable and
                     self.f_t.holding_site(site).workstation_id == station), chosen)

    # ------------------------------------------------------------------
    # 着色主流程
    # ------------------------------------------------------------------
    def _reserve_held(self) -> None:
        """把携带类与已着色版本的位置重新登记为本轮预留的占位区间 R_p(t)。"""
        for vreg, site_id in self.allocation.items():
            if not isinstance(site_id, int) or self._carried_site(vreg) == site_id:
                continue
            start, end = self._holding_span(vreg)
            self.f_t.reserve_interval(site_id, start, end)

    def _holding_span(self, vreg: str):
        """版本占住位置的起止时刻；没有生命期信息时退化为一个很短但仍存在的区间。"""
        start, end = self.vreg_liveness.get(vreg, (0.0, 0.0))
        if end <= start:
            end = start + 1e-9
        return start, end

    def _hold(self, vreg: str, site_id: int) -> None:
        """登记一次落位：既记分配结果，也记它占住该位置的时间区间。"""
        start, end = self._holding_span(vreg)
        self.allocation[vreg] = site_id
        if self._carried_site(vreg) != site_id:
            self.f_t.reserve_interval(site_id, start, end)
        self._colored.add(vreg)
        self.waiting.discard(vreg)

    def color(self, time: float = 0.0) -> ColorRoundResult:
        """在 F_t 的时刻 time 上执行一轮完整着色。"""
        self.f_t.state.time = float(time)
        interference = self._build_interference()
        if self.manage_reservations:
            self.f_t.release_reservations()
            self._reserve_held()
        round_result = ColorRoundResult(time=float(time))

        for vreg in sorted(self.vregs, key=lambda v: self._order_key(v, interference)):
            if vreg in self._colored or vreg in self._consumed:
                continue
            usable = self._candidate_sites(vreg)[0]
            if not usable:
                self.waiting.add(vreg)
                round_result.waiting.append(vreg)
                self._wait_reason(vreg, interference, round_result)
                continue
            chosen = self._pick_site(vreg, usable)
            self._hold(vreg, chosen)
            round_result.colored[vreg] = chosen

        self.rounds.append(round_result)
        return round_result

    def _wait_reason(self, vreg: str, interference: Dict[str, Set[str]],
                     round_result: ColorRoundResult) -> None:
        _, blocked, reason = self._candidate_sites(vreg)
        free_but_taken = [
            site_id for site_id in self._candidate_sites(vreg)[0]
            if any(self.allocation.get(n) == site_id for n in interference.get(vreg, set()))
        ]
        round_result.blocked_on[vreg] = sorted(set(blocked) | set(free_but_taken))
        if free_but_taken:
            round_result.unavailable_reason[vreg] = "all_candidates_taken_by_conflicting_versions"
        else:
            round_result.unavailable_reason[vreg] = reason or "no_candidate"

    def retry(self, time: float) -> ColorRoundResult:
        """
        资源释放后对 WAIT 中的版本及其残余干涉邻域重新着色。

        只重算等待中的版本：已着色且未受影响的版本保持不动，这就是"区域性"最小扰动的来源。
        """
        waiting_before = set(self.waiting)
        self.waiting.clear()
        interference = self._build_interference()
        neighbourhood: Set[str] = set(waiting_before)
        for vreg in waiting_before:
            neighbourhood.update(interference.get(vreg, set()))
        reschedule = [v for v in sorted(self.vregs, key=lambda v: self._order_key(v, interference))
                      if v in waiting_before or v in neighbourhood]

        self.f_t.state.time = float(time)
        # 保留已着色版本的预留，只撤销等待邻域的旧选择
        if self.manage_reservations:
            self.f_t.release_reservations()
            self._reserve_held()

        round_result = ColorRoundResult(time=float(time))
        for vreg in reschedule:
            if vreg in self.allocation or vreg in self._consumed:
                continue
            usable = self._candidate_sites(vreg)[0]
            if not usable:
                self.waiting.add(vreg)
                round_result.waiting.append(vreg)
                self._wait_reason(vreg, interference, round_result)
                continue
            chosen = self._pick_site(vreg, usable)
            self._hold(vreg, chosen)
            round_result.colored[vreg] = chosen

        self.rounds.append(round_result)
        return round_result

    # ------------------------------------------------------------------
    # 物料被取走
    # ------------------------------------------------------------------
    def consume(self, vregs: Iterable[str]) -> List[int]:
        """
        物料被下游工序取走：释放它占的持有位置。

        这不是"提前结束"，而是车间推进的正常结果——料走了，位置自然空出来，
        此前因为没位置而等待的版本才可能落位。
        """
        freed: List[int] = []
        for vreg in vregs:
            self._consumed.add(vreg)
            site = self.allocation.pop(vreg, None)
            self._colored.discard(vreg)
            self.waiting.discard(vreg)
            if not isinstance(site, int):
                continue
            freed.append(site)
            start, end = self._holding_span(vreg)
            intervals = self.f_t.state.holding_intervals.get(site)
            if not intervals:
                continue
            for index, interval in enumerate(intervals):
                if interval[0] == start and interval[1] == end:
                    intervals.pop(index)
                    break
        return freed

    def consumed_versions(self) -> Set[str]:
        return set(self._consumed)

    def earliest_finishing(self, count: int) -> List[str]:
        """按占位结束时刻排序，取最早结束的若干已落位版本。"""
        placed = [v for v, site in self.allocation.items() if isinstance(site, int)]
        placed.sort(key=lambda v: self.vreg_liveness.get(v, (0.0, 0.0))[1])
        return placed[:max(0, count)]

    # ------------------------------------------------------------------
    # 跨工位搬运
    # ------------------------------------------------------------------
    def _insert_cross_ws_moves(self) -> None:
        """
        仅当相邻工序在 Target Lowering 之后绑定到不同工位时，才插入一次物料搬运。

        不存在"人工 spill 槽位"的概念：搬运的目标位置必须本身是可行持有位置。
        """
        for block in self.cfg.blocks:
            if self._is_exception_path_block(block):
                continue
            instr_ws: Dict[Tuple[int, int], int] = {}
            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue
                ws = self._instr_workstation(instr)
                if ws is not None:
                    instr_ws[(block.id, idx)] = ws

            moves: List[Tuple[int, OpInstr]] = []
            local_ws: Dict[str, int] = {}
            for idx, instr in enumerate(block.instructions):
                if not isinstance(instr, OpInstr):
                    continue
                ws_target = instr_ws.get((block.id, idx))
                if ws_target is None:
                    continue
                for pos in self._material_arg_indices(instr.op_type, len(instr.args)):
                    if pos >= len(instr.args):
                        continue
                    vreg = instr.args[pos]
                    if not self._is_vreg(vreg) or vreg not in self.allocation:
                        continue
                    site = self.f_t.holding_site(self.allocation[vreg])
                    if site is None:
                        continue
                    if local_ws.get(vreg, site.workstation_id) == ws_target:
                        continue
                    dst = self._pick_landing_site(instr, ws_target, vreg)
                    if dst is None:
                        continue
                    moves.append((idx, OpInstr(op_type="Move", target=None, args=[vreg, str(dst)])))
                    local_ws[vreg] = ws_target

            moves.sort(key=lambda item: item[0], reverse=True)
            for insert_idx, move_instr in moves:
                block.instructions.insert(insert_idx, move_instr)

    def _instr_workstation(self, instr: OpInstr) -> Optional[int]:
        dominant = None
        if instr.target and self._is_vreg(instr.target):
            dominant = instr.target
        else:
            for extra in instr.implicit_targets or []:
                if isinstance(extra, str) and self._is_vreg(extra) and extra in self.allocation:
                    dominant = extra
                    break
        if dominant is None:
            for pos in self._material_arg_indices(instr.op_type, len(instr.args)):
                if pos < len(instr.args) and self._is_vreg(instr.args[pos]):
                    dominant = instr.args[pos]
                    break
        site_id = self.allocation.get(dominant) if dominant else None
        site = self.f_t.holding_site(site_id) if site_id is not None else None
        if site is not None:
            return site.workstation_id

        # 输出物料还没有位置时，用该指令自身能否在某工位执行来定
        lowered: Optional[LoweredOperation] = self.lowering.operations.get(id(instr))
        if lowered is not None and lowered.candidate_workstations:
            return lowered.candidate_workstations[0]
        return None

    def _pick_landing_site(self, instr: OpInstr, ws_target: int, vreg: str) -> Optional[int]:
        """搬运落点：优先取本指令输出物料的位置，否则取目标工位上第一个可行位置。"""
        if instr.target and self._is_vreg(instr.target):
            site_id = self.allocation.get(instr.target)
            if site_id is not None:
                site = self.f_t.holding_site(site_id)
                if site is not None and site.workstation_id == ws_target:
                    return site_id
        eligible = set(self._eligible_sites(vreg))
        for site_id in self.f_t.holding_sites_of_workstation(ws_target):
            if site_id in eligible:
                return site_id
        return None

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    def run(self, time: float = 0.0, max_wait_rounds: int = 0) -> ColoringOutcome:
        """
        执行着色。max_wait_rounds > 0 时允许在**没有新信息**的情况下原地重试；
        正常使用应由调用方在资源真正释放后调用 retry()。
        """
        outcome = ColoringOutcome()
        result = self.color(time)
        outcome.rounds.append(result)
        if result.waiting:
            outcome.wait_events += 1
        rounds_left = max_wait_rounds
        while self.waiting and rounds_left > 0:
            result = self.retry(time)
            outcome.rounds.append(result)
            outcome.wait_events += 1
            rounds_left -= 1

        outcome.allocation = {
            vreg: site for vreg, site in self.allocation.items()
            if isinstance(site, int)
        }
        outcome.waiting = sorted(self.waiting)
        self._finalize()
        return outcome

    def _finalize(self) -> None:
        """绑定 PALU 并生成跨工位搬运；重复调用不会重复插入。"""
        if self._finalized:
            return
        self._assign_palus()
        if self.emit_moves:
            self._insert_cross_ws_moves()
        self._finalized = True

    def allocation_map(self) -> Dict[str, int]:
        """与既有 alloc_map 契约一致：物料版本 -> 物理持有位置 id（永不出现 memory）。"""
        self._finalize()
        return {vreg: site for vreg, site in self.allocation.items() if isinstance(site, int)}


@dataclass
class WorksetColorResult:
    """整个工作集的着色结果。"""

    allocation: Dict[str, int] = field(default_factory=dict)
    waiting: List[str] = field(default_factory=list)
    per_order: Dict[str, ColoringOutcome] = field(default_factory=dict)

    def snapshot(self) -> Dict[str, object]:
        return {
            "assigned": len(self.allocation),
            "waiting": len(self.waiting),
            "orders": len(self.per_order),
        }


class WorksetColorer:
    """
    对当前工作集 W_t 做一次联合着色。

    多个订单共享同一个 F_t，因此共享同一张预留账本：先着色的订单占住的库位，
    后面的订单必须让开。每个订单内部的冲突仍由它自己的干涉图决定，
    这样"哪些版本互相冲突"与"整个车间还剩多少位置"是分开判定的。
    """

    def __init__(self, f_t: FactoryTargetMachine,
                 selector: Optional["SiteSelector"] = None):
        self.f_t = f_t
        # 落位选择器：合格性判定共用，只有"从合格库里挑哪一个"随方法而变。
        self.selector = selector or GraphColoringSelector()
        self._last_colorers: Dict[str, ResidualGraphColorer] = {}

    def color(self, fragments, lowerings: Mapping[str, LoweringResult],
              pinned_locations: Optional[Mapping[str, int]] = None,
              time: float = 0.0,
              affected_vregs: Optional[Mapping[str, Iterable[str]]] = None,
              handler_vregs: Optional[Iterable[str]] = None) -> WorksetColorResult:
        """
        对整个工作集（或指定订单的指定版本子集）着色。

        `affected_vregs` 为空时是初次落位：每个订单的待定版本都要着色，携带版本用运行时位置。
        给出 `affected_vregs` 时是区域性重新落位：只有列出的版本重新决定位置，
        其余版本由 `pinned_locations` 钉在原位，不参与决策。
        """
        self.f_t.state.time = float(time)
        self.f_t.release_reservations()
        pinned = dict(pinned_locations or {})

        result = WorksetColorResult(allocation=dict(pinned))
        prepared = []
        physical = {v.rsplit("_", 1)[0]: site
                    for v, site in self.f_t.state.wip_location.items()}
        for fragment in fragments:
            lowering = lowerings.get(fragment.order_id)
            if lowering is None:
                continue

            if affected_vregs is None:
                target_vregs = set(fragment.pending_vregs)
                fragment_pinned = {
                    vreg: site for vreg, site in pinned.items()
                    if vreg in fragment.carried_vregs
                }
            else:
                target_vregs = set(affected_vregs.get(fragment.order_id, ()))
                owned = fragment_all_vregs(fragment)
                fragment_pinned = {
                    vreg: site for vreg, site in pinned.items()
                    if vreg not in target_vregs and vreg in owned
                }
            for vreg in target_vregs:
                result.allocation.pop(vreg, None)
            if not target_vregs and not fragment_pinned:
                continue

            colorer = self._last_colorers.get(fragment.order_id)
            if target_vregs or colorer is None:
                colorer = ResidualGraphColorer(
                    fragment.cfg,
                    fragment.mdg,
                    self.f_t,
                    lowering,
                    selector=self.selector,
                    residual_vregs=target_vregs,
                    pinned_locations=fragment_pinned,
                    manage_reservations=False,
                    handler_vregs=set(handler_vregs or ()) & target_vregs,
                    emit_moves=False,
                )
                self._last_colorers[fragment.order_id] = colorer
            for vreg, site in fragment_pinned.items():
                if physical.get(vreg.rsplit("_", 1)[0]) != site:
                    start, end = colorer._holding_span(vreg)
                    self.f_t.reserve_interval(site, start, end)
            if target_vregs:
                prepared.append((fragment.order_id, colorer))

        # 区域外的既有计划先登记容量，再给本轮目标分配。
        for order_id, colorer in prepared:
            outcome = colorer.run(time)
            result.per_order[order_id] = outcome
            result.allocation.update(outcome.allocation)
            result.waiting.extend(outcome.waiting)
        return result

    def colorers(self) -> Dict[str, "ResidualGraphColorer"]:
        """最近一次着色所用的、按订单拆分的着色器（供推进与再次落位使用）。"""
        return dict(self._last_colorers)


def fragment_all_vregs(fragment) -> Set[str]:
    """片段内可能出现的全部物料版本（待定 + 携带 + 冻结）。"""
    return set(fragment.pending_vregs) | set(fragment.carried_vregs) | set(fragment.frozen_vregs)
