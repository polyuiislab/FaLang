"""Combine fixed exception-handler plans with CBC-selected regional residual recompilation."""
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


    def apply_state_change(self, event: UncertaintyEvent, factory_state=None) -> Optional[float]:

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


    def detect_region(
        self,
        event: UncertaintyEvent,
        workset: ResidualWorkset,
        allocation: Mapping[str, int],
        current_time: float = 0.0,
        seeds: Optional[Set[str]] = None,
    ) -> AffectedRegion:
        """Use invalidated resources when seeds is None; an explicit empty seed set selects no region."""
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
            # Running versions retain their physical locations as carried material.
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
        """Find versions whose assigned site or unbound candidate set is invalidated by the event."""
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
        """Check whether resource recovery precedes the operation release time."""
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


    def order_residual_versions(self, workset, order_id: str) -> Set[str]:

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


@dataclass
class HandlerApplication:


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
    """Activate precompiled catch actions and shift their relative timings to the event time."""

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
        """Select the exception entry for the triggering program point."""
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
        """Collect recovery operations across all catch continuation blocks, visiting each block once."""
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
        """Return material versions produced along the activated handler path."""
        vregs: Set[str] = set()
        for instr in self.recovery_instructions(exception_type):
            vregs.update(instr.implicit_targets or [])
            if instr.target:
                vregs.add(instr.target)
            vregs.update(arg for arg in instr.args if isinstance(arg, str))
        return vregs

    def apply(self, exception_type: str, t_exc: float) -> Optional[HandlerApplication]:
        """Shift precompiled recovery durations from the compile-time trigger to the actual event time."""
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


@dataclass
class HandlingOutcome:


    event: UncertaintyEvent
    attributes: EventAttributes
    plan: HandlingPlan
    handler_application: Optional[HandlerApplication] = None

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
    """Coordinate state changes, handler activation, regional recompilation, and waiting versions."""

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


        self.region_rule = "containment"
        self.handlers = dict(handlers or {})
        self.detector = RegionalResidualRecompilation(f_t, dict(lowerings), max_hops=max_hops)
        self._lowerings: Dict[str, LoweringResult] = dict(lowerings)
        self.allocation: Dict[str, int] = {}
        self.waiting: Set[str] = set()
        self.history: List[HandlingOutcome] = []


    @property
    def lowerings(self) -> Dict[str, LoweringResult]:
        return self._lowerings

    @lowerings.setter
    def lowerings(self, value: Mapping[str, LoweringResult]) -> None:
        """Share current lowering results with the region detector."""
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


    def handle(self, event: UncertaintyEvent,
               allocation: Optional[Mapping[str, int]] = None) -> HandlingOutcome:
        if allocation is not None:
            self.set_allocation(allocation)
        current = dict(self.allocation)

        # Evaluate CBC against the factory state after applying the event.


        if event.type.invalidates_resource:
            self.detector.apply_state_change(event)

        attributes = self.attributes_of(event, current)
        plan = self.policy.plan(attributes)
        outcome = HandlingOutcome(event=event, attributes=attributes, plan=plan)

        if plan.use_handler:
            outcome.handler_application = self._activate_handler(event)
            # Handler activation installs the resource plan fixed during initial compilation.
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
                # Events without resource invalidation select the affected order residual as the seed region.


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


    def _baseline_boundary(self, event: UncertaintyEvent, seeds):
        """Apply the selected order, workstation-group, or workset boundary before regional recompilation."""
        fragments = list(self.workset.fragments) if self.workset else []
        if self.region_rule == "event_order":

            return self.detector.order_residual_versions(self.workset, event.order_id)
        if self.region_rule == "machine_group":
            # Holding sites map to their workstation; a shared logistics failure spans workstations.
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


            picked = set()
            for fragment in fragments:
                picked |= set(fragment.pending_vregs) | set(fragment.carried_vregs)
            return picked
        return seeds

    def _bind_handler_material(self, event: UncertaintyEvent,
                                application: Optional[HandlerApplication]) -> None:
        """Install the initial fixed handler bindings."""
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
        """Evaluate anticipation, handler executability, and coverage in the current context."""
        current = dict(self.allocation if allocation is None else allocation)
        handler = self.handler_for(event)
        exception_type = event.type.exception_type
        anticipated = bool(handler is not None and handler.handles(exception_type))

        attributes = EventAttributes(anticipated=anticipated)
        if not anticipated:

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
        """Check resources for every recovery action, including catch continuation blocks."""
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


    def release(self, event: UncertaintyEvent, allocation: Optional[Mapping[str, int]] = None):
        """Restore resource availability and retry waiting versions."""
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
