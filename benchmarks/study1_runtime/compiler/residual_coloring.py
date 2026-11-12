"""Allocate residual material versions to feasible holding sites, sharing interval reservations across orders."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from compiler.ir import OpInstr, PhiInstr, BasicBlock, ControlFlowGraph
from compiler.register_alloc import RegisterAllocator
from compiler.factory_target_machine import FactoryTargetMachine, FactoryState
from compiler.target_lowering import LoweredOperation, LoweringResult


@dataclass
class ColorRoundResult:


    time: float
    colored: Dict[str, int] = field(default_factory=dict)
    waiting: List[str] = field(default_factory=list)
    # Waiting version to blocked candidate sites.
    blocked_on: Dict[str, List[int]] = field(default_factory=dict)

    unavailable_reason: Dict[str, str] = field(default_factory=dict)


@dataclass
class ColoringOutcome:


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


# All selectors share capability, interval-capacity, and availability checks.


class SiteSelector:


    name = "base"
    description = ""

    def sort_key(self, vreg: str, site_id: int, colorer):
        raise NotImplementedError


class GraphColoringSelector(SiteSelector):
    """Rank feasible sites by predicted transfers and holding-site load."""

    name = "graph_coloring"
    description = "transport anticipation + load (residual graph coloring)"

    def sort_key(self, vreg, site_id, colorer):
        site = colorer.f_t.holding_site(site_id)
        return (colorer._transfer_count(vreg, site_id),
                colorer._site_load(site_id, vreg),
                site.workstation_id, site_id)


class FirstFitSelector(SiteSelector):
    """Choose the first feasible holding site."""

    name = "first_fit"
    description = "first eligible site (capacity only)"

    def sort_key(self, vreg, site_id, colorer):
        return (site_id,)


class TransportJointSelector(SiteSelector):
    """Rank feasible sites by predicted transfers."""

    name = "machine_transport_joint"
    description = "transport anticipation only, no load awareness"

    def sort_key(self, vreg, site_id, colorer):
        site = colorer.f_t.holding_site(site_id)
        return (colorer._transfer_count(vreg, site_id),
                site.workstation_id, site_id)


class LoadBalanceSelector(SiteSelector):
    """Rank feasible sites by holding-site load."""

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
    """Color residual material lifetimes using target capabilities and current factory capacity."""

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
        # Workset coloring owns the reservation ledger shared by its per-order colorers.
        self.manage_reservations = manage_reservations

        self.pinned: Dict[str, int] = dict(pinned_locations or {})
        self._physical_sites = {name.rsplit("_", 1)[0]: site
                                for name, site in self.f_t.state.wip_location.items()}

        super().__init__(cfg, mdg, None, f_t._tm)
        if residual_vregs is not None:


            allowed = set(residual_vregs) | set(handler_vregs or ())
            self.vregs = [v for v in self.vregs if v in allowed]
        if handler_vregs:


            self._include_handler_vregs(set(handler_vregs))

        self.waiting: Set[str] = set()
        self._colored: Set[str] = set()

        self._consumed: Set[str] = set()
        self._finalized = False
        self.rounds: List[ColorRoundResult] = []

        for vreg, site_id in self.pinned.items():
            self.allocation[vreg] = site_id
            self._colored.add(vreg)
            if self.manage_reservations and self._carried_site(vreg) != site_id:
                start, end = self._holding_span(vreg)
                self.f_t.reserve_interval(site_id, start, end)


    def _build_instr_time_map(self) -> None:

        self.instr_times: Dict[int, Tuple[float, float]] = dict(getattr(self, "instr_times", {}) or {})

    def _include_handler_vregs(self, handler_vregs: Set[str]) -> None:
        """Include material versions from activated catch and compensate paths."""
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
        """Bound handler material lifetimes from the trigger through the last recovery use."""
        from compiler.mdg import MDGNode  # noqa: F401

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

        return {}

    def _compute_liveness_from_mdg(self) -> None:
        """Derive holding lifetimes by ASAP propagation over the MDG."""
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


    def _build_interference(self) -> Dict[str, Set[str]]:
        """Build lifetime-overlap edges with a sweep line."""
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

        # A consuming operation transfers ownership between versions of the same physical material.
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


    def _eligible_sites(self, vreg: str) -> List[int]:

        sites = self.lowering.eligible_sites.get(vreg)
        if sites is not None:
            return list(sites)
        return sorted(site.storage_id for site in self.f_t.holding_sites())

    def _candidate_sites(self, vreg: str) -> Tuple[List[int], List[int], str]:
        """Return usable sites, blocked sites, and a reason, checking capacity throughout each holding interval."""
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
            # In-place processing changes the material version without adding physical occupancy.
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
        """Return peak holding load including reservations from other orders."""
        start, end = self._holding_span(vreg)
        return self.f_t.capacity_of(site_id) - self.f_t.free_capacity_over(site_id, start, end)

    def _covering_sites(self, vreg: str) -> Set[int]:
        """Find sites whose workstation supports downstream consuming operations."""
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
        """Count transfers for incoming material and the next operation; same-site handoffs cost zero."""
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
        """Delegate selection among feasible sites to the configured selector."""
        chosen = min(usable, key=lambda site_id: self.selector.sort_key(vreg, site_id, self))
        station = self.f_t.holding_site(chosen).workstation_id
        family = vreg.rsplit("_", 1)[0]
        # Consecutive versions at one workstation can retain the same holding site.
        related = [self._carried_site(vreg)] + [
            site for name, site in self.allocation.items() if name.rsplit("_", 1)[0] == family]
        return next((site for site in related if site in usable and
                     self.f_t.holding_site(site).workstation_id == station), chosen)


    def _reserve_held(self) -> None:
        """Restore interval reservations for carried and already allocated versions."""
        for vreg, site_id in self.allocation.items():
            if not isinstance(site_id, int) or self._carried_site(vreg) == site_id:
                continue
            start, end = self._holding_span(vreg)
            self.f_t.reserve_interval(site_id, start, end)

    def _holding_span(self, vreg: str):
        """Return the holding interval, using a positive epsilon when no duration is available."""
        start, end = self.vreg_liveness.get(vreg, (0.0, 0.0))
        if end <= start:
            end = start + 1e-9
        return start, end

    def _hold(self, vreg: str, site_id: int) -> None:

        start, end = self._holding_span(vreg)
        self.allocation[vreg] = site_id
        if self._carried_site(vreg) != site_id:
            self.f_t.reserve_interval(site_id, start, end)
        self._colored.add(vreg)
        self.waiting.discard(vreg)

    def color(self, time: float = 0.0) -> ColorRoundResult:

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
        """Recolor waiting versions after resource release while preserving unaffected allocations."""
        waiting_before = set(self.waiting)
        self.waiting.clear()
        interference = self._build_interference()
        neighbourhood: Set[str] = set(waiting_before)
        for vreg in waiting_before:
            neighbourhood.update(interference.get(vreg, set()))
        reschedule = [v for v in sorted(self.vregs, key=lambda v: self._order_key(v, interference))
                      if v in waiting_before or v in neighbourhood]

        self.f_t.state.time = float(time)

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


    def consume(self, vregs: Iterable[str]) -> List[int]:
        """Release the holding reservation when a downstream operation consumes the material."""
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

        placed = [v for v, site in self.allocation.items() if isinstance(site, int)]
        placed.sort(key=lambda v: self.vreg_liveness.get(v, (0.0, 0.0))[1])
        return placed[:max(0, count)]


    def _insert_cross_ws_moves(self) -> None:
        """Insert material transfers for adjacent operations assigned to different workstations."""
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


        lowered: Optional[LoweredOperation] = self.lowering.operations.get(id(instr))
        if lowered is not None and lowered.candidate_workstations:
            return lowered.candidate_workstations[0]
        return None

    def _pick_landing_site(self, instr: OpInstr, ws_target: int, vreg: str) -> Optional[int]:
        """Prefer the output location, then a feasible site at the destination workstation."""
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


    def run(self, time: float = 0.0, max_wait_rounds: int = 0) -> ColoringOutcome:
        """Run coloring; callers normally invoke retry after a resource-state change."""
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
        """Bind PALUs and emit cross-workstation moves once."""
        if self._finalized:
            return
        self._assign_palus()
        if self.emit_moves:
            self._insert_cross_ws_moves()
        self._finalized = True

    def allocation_map(self) -> Dict[str, int]:
        """Return material-version to physical-holding-site assignments."""
        self._finalize()
        return {vreg: site for vreg, site in self.allocation.items() if isinstance(site, int)}


@dataclass
class WorksetColorResult:


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
    """Color orders against a shared factory reservation ledger."""

    def __init__(self, f_t: FactoryTargetMachine,
                 selector: Optional["SiteSelector"] = None):
        self.f_t = f_t

        self.selector = selector or GraphColoringSelector()
        self._last_colorers: Dict[str, ResidualGraphColorer] = {}

    def color(self, fragments, lowerings: Mapping[str, LoweringResult],
              pinned_locations: Optional[Mapping[str, int]] = None,
              time: float = 0.0,
              affected_vregs: Optional[Mapping[str, Iterable[str]]] = None,
              handler_vregs: Optional[Iterable[str]] = None) -> WorksetColorResult:
        """Color pending versions, or the affected subset, retaining pinned and carried locations."""
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

        # Reserve allocations outside the region before allocating its pending versions.
        for order_id, colorer in prepared:
            outcome = colorer.run(time)
            result.per_order[order_id] = outcome
            result.allocation.update(outcome.allocation)
            result.waiting.extend(outcome.waiting)
        return result

    def colorers(self) -> Dict[str, "ResidualGraphColorer"]:

        return dict(self._last_colorers)


def fragment_all_vregs(fragment) -> Set[str]:

    return set(fragment.pending_vregs) | set(fragment.carried_vregs) | set(fragment.frozen_vregs)
