"""Discrete-event PSE simulation with resource allocation and uncertainty responses."""

from __future__ import annotations

import heapq
import math
import time
from collections import Counter
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from case_study.domains import DOMAIN_KEYS, materialize_all, program_generator, spec_of, target_model_path
from case_study.domains.orders import DomainOrder, DomainOrderGenerator
from compiler.factory_target_machine import AVAIL_HOLDING, AVAIL_LOGISTICS, AVAIL_WORKSTATION, FactoryState
from compiler.ir import ControlFlowGraph, OpInstr
from compiler.target_lowering import TargetLowering
from compiler.regional_residual import (
    PrecompiledExceptionHandler,
    UncertaintyCoordinator,
    attributes_handler_vregs,
)
from compiler.residual_coloring import WorksetColorer, make_site_selector
from compiler.residual_ir import (
    CompiledOrderProgram,
    ResidualRecord,
    ResidualWorkset,
    WorksetComposer,
    _is_handler_block,
)
from compiler.uncertainty import (
    GROUP_A,
    GROUP_B,
    TARGET_HOLDING,
    TARGET_LOGISTICS,
    TARGET_WORKSTATION,
    ResponsePolicy,
    UncertaintyEvent,
    UncertaintyStream,
)
from core.compiler_proxy import CompilerProxy

RESULTS_DIR = Path(__file__).resolve().parents[1] / "study2_results"


# ----------------------------------------------------------------------
# 配置
# ----------------------------------------------------------------------
@dataclass
class Study2Config:
    """Shared configuration for the paper's allocation and uncertainty-response studies."""

    domain: str = 'garment_continuation_compact'
    orders: int = 50
    seeds: Sequence[int] = (182000, 182001, 182002, 182003, 182004, 182005, 182006, 182007, 182008, 182009, 182010, 182011, 182012, 182013, 182014, 182015, 182016, 182017, 182018, 182019, 182020, 182021, 182022, 182023, 182024, 182025, 182026, 182027, 182028, 182029, 182030, 182031, 182032, 182033, 182034, 182035, 182036, 182037, 182038, 182039, 182040, 182041, 182042, 182043, 182044, 182045, 182046, 182047, 182048, 182049)
    load: str = 'high'
    target_utilization: float = 0.6
    wip_cap: int = 8
    workstation_execution_slots: Optional[int] = None
    events_per_order: float = 0.8
    resource_events_per_order: float = 2
    resource_event_weights: Tuple[int, int, int] = (9, 1, 0)
    resource_events_at_dispatch: bool = True
    group_a_share: float = 0.2
    policies: Tuple[ResponsePolicy, ...] = (ResponsePolicy.REACTIVE, ResponsePolicy.PREDEFINITION, ResponsePolicy.HYBRID,)
    repair_delay: Tuple[float, float] = (80, 80)
    processing_cv: float = 0.15
    transport_cv: float = 0.2
    transport_factor: float = 1.0
    transport_setup: float = 4
    transport_per_step: float = 0.75
    due_slack_factor: float = 2
    anticipated_only: bool = False
    site_selector: str = 'graph_coloring'
    region_rule: str = 'containment'

    def snapshot(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["policies"] = [p.value for p in self.policies]
        payload["seeds"] = list(self.seeds)
        return payload


@dataclass
class SimulationResult:
    domain: str
    policy: str
    seed: int
    load: str
    orders: int
    orders_per_hour: float
    concurrent_peak: int
    makespan: float
    mean_flow_time: float
    mean_tardiness: float
    on_time_delivery_rate: float
    resource_utilization: float
    # 投料区间（首单到达 → 末单到达）口径的负荷强度。它不随策略变，
    # 只用来描述场景被投料的强度，不参与方法比较。
    feeding_window_utilization: float
    # 处理器处置动作实际占用的设备分钟数（已计入上面的利用率分子）
    recovery_busy_time: float
    # 车间量重规划代价：被改判落位、需要重新下达指令并重新搬运的物料版本数；
    # 以及因此被打断流转的在制工单数。二者直接对应现场运营成本的上升。
    rehandled_versions: int
    disturbed_orders: int
    blocking_time: float
    waiting_time: float
    transport_time: float
    setup_time: float
    # 机制指标
    uncertainty_events: int
    dispatch_repair_requests: int
    handler_activations: int
    recompilations: int
    recompilation_time_s: float
    rebound_versions: int
    blocked_orders: int
    waiting_versions_peak: int
    colorings: int
    coloring_time_s: float
    compile_time_s: float
    wall_clock_s: float


# ----------------------------------------------------------------------
# 订单在仿真中的运行时状态
# ----------------------------------------------------------------------
@dataclass
class OrderRuntime:
    order: DomainOrder
    program: object
    compiled: CompiledOrderProgram
    lowering: object               # Target Lowering 结果
    ops: List[object]              # 按工序顺序排列的 OpInstr
    # 与 ops 一一对应的逻辑工序号（OperationKey.seq）。一条逻辑工序可能对应
    # 多条实体指令，所以 ops 的下标不能直接当作 seq 交给 Workset Composer。
    op_seqs: List[int] = field(default_factory=list)
    completed_seq: int = -1
    executing: Optional[int] = None
    executing_until: float = 0.0
    executing_station: Optional[int] = None
    start_time: Optional[float] = None
    finish_time: Optional[float] = None
    transport_until: float = 0.0
    material_location: Optional[int] = None
    due_time: float = 0.0
    # 此刻真实存在于车间的物料版本 -> 它占用的库位。着色只应该为**实际存在**的
    # 物料保留位置：把整段残余程序的所有版本都当成同时占位，会让车间的库位数
    # 与在制品数脱节，从而出现工厂明明有空位、版本却永远等不到落位的情况。
    held: Dict[str, int] = field(default_factory=dict)
    # 当前在制工序的产出落位（完工后成为 held 的内容）
    op_site: Optional[int] = None
    # 该订单是否有物料正在被 AGV 搬运（在途期间不能开工）
    in_transit: bool = False
    # 搬运一旦发车，目的库位锁定到本道工序完成。
    dispatch_site: Optional[int] = None
    # 不确定性处置造成的停工期：区域重编译期间被冻结，或兜不住的版本等待资源恢复
    blocked_until: float = 0.0
    # 未处置的缺陷：工装偏位 / 质量偏差被跳过之后，缺陷留在制品上，
    # 直到检验环节才显形并触发返工。缺料不在此列——那是"料没到"，
    # 会直接卡住生产，不会做出带缺陷的制品。
    pending_defects: List[UncertaintyEvent] = field(default_factory=list)
    # 该订单此刻有一份**可执行的预编译处置方案**，有效期到这个时刻为止。
    # 有方案的订单在设备故障时可以按方案改派到替代工位；没有方案的订单
    # 只能等原工位修好——这正是"预定义"相对"没有预定义"的实际价值。
    reroute_until: float = 0.0
    # 本订单当前这段"等待"的起点。等待按**一次一段**计：进入等待时记起点，
    # 开工时结算一次。逐轮把剩余等待累加会把同一段等待重复计几十上百遍。
    waiting_since: Optional[float] = None
    # 处理器被启用、但其物料落不下去的那些版本。只要它还非空，该订单的
    # 处置动作就起不来，只能等资源空出来——等待的长短由资源本身决定，不是常数。
    blocked_handler_vregs: set = field(default_factory=set)
    fixed_handler_locations: dict = field(default_factory=dict)
    fixed_handler_binding: Optional[object] = None
    # 该订单程序里预定义的异常处理器（try-catch 的 catch 分支）
    handler: Optional[object] = None


class LineSimulator:
    """服装装配线上的离散事件仿真。"""

    def __init__(self, config: Study2Config, *, model_path: Optional[Path] = None):
        self.config = config
        self.spec = spec_of(config.domain)
        self.generator = program_generator(config.domain)
        self.proxy = CompilerProxy(str(
            model_path if model_path is not None else target_model_path(config.domain)))
        self.f_t = self.proxy.build_factory_target_machine()
        self.workstation_slots = self._workstation_slots()
        self.ws_coords = {ws.workstation_id: (ws.row, ws.col) for ws in self.spec.workstations}
        # 事件属性按触发时的处理器与现场状态记录。
        self.attribute_records: List[Dict[str, object]] = []
        # 补充实验的只读观察器；默认关闭，不改变原实验决策与输出格式。
        self.event_response_observer = None
        self.event_batch_observer = None
        # 外生固定资源日历可在没有当前异常工单时失效；此观察器不参与决策。
        self.resource_outage_observer = None
        self.resource_outage_records = []
        # 可选的工序启动故障注入器；正常实验不启用。
        self.dispatch_uncertainty_factory = None
        # 已决定的落位跨仿真步保留，事件改判后更新。
        self._planned: Dict[str, int] = {}
        self._station_busy_until: Dict[int, List[float]] = {}
        self.reassignment_records = []
        # 仅观察实际发车与等待，不参与落位、资源选择或仿真时钟推进。
        self.collect_transport_records = False
        self.transport_records = []
        self.transport_requests = {}
        self.reassignment_actions = next((rule.recovery for rule in self.spec.protections
            if rule.exception_type == "MachineFailure" and
            rule.recovery == (("Adjust", "Recovery_PM", "Operator_Reassign"),)), ())

    # ------------------------------------------------------------------
    def _recovery_duration(self, event: UncertaintyEvent) -> float:
        """
        非资源态事件的消解时长 = 该不确定性的预定义恢复动作本身要花的时间。

        缺料靠补料消解、质量偏差靠返工消解，时长就写在工艺规格里
        （`Fabric_Replenish`、`Garment_Rework` 之类的动作时间）。
        没有对应保护规则时，退回到规格中全部 Adjust 动作的最大时长——
        仍然是规格里的数，不是新引入的常数。
        """
        spec = self.spec
        for rule in spec.protections:
            if (rule.uncertainty_key == getattr(event.type, "key", None)
                    or rule.exception_type == event.type.exception_type):
                times = [
                    times for times in (
                        spec.action_time(op_type, rp) for op_type, _role, rp in rule.recovery
                    ) if times
                ]
                if times:
                    return float(max(times))
        adjust = [
            times for times in (
                spec.action_time(action.op_type, action.rp)
                for action in spec.actions if action.op_type == "Adjust"
            ) if times
        ]
        return float(max(adjust)) if adjust else 0.0

    def _workstation_slots(self) -> Dict[int, int]:
        ics = self.proxy.target_model.data.get("instruction_capability_set") or {}
        palus = {p.get("palu_id"): p for p in ics.get("palu_definitions", [])}
        logistics_ids = {
            pid for pid, definition in palus.items()
            if "AGV" in str(definition.get("device_type", "")).upper()
            or "LOGISTIC" in str(definition.get("device_type", "")).upper()
        }
        slots: Dict[int, int] = {}
        mapping = (self.proxy.target_model.data.get("addressing_model") or {}).get(
            "workstation_mapping", [])
        for entry in mapping:
            ws_id = entry.get("workstation_id")
            assigned = entry.get("assigned_palus") or []
            slots[ws_id] = (self.config.workstation_execution_slots
                           if self.config.workstation_execution_slots is not None
                           else max(1, sum(1 for pid in assigned if pid not in logistics_ids)))
        return slots

    def _station_loads(self, orders: Sequence[OrderRuntime]) -> Dict[int, float]:
        """每张订单在各工位上的名义占用时长之和（按可选工位数均摊）。"""
        loads: Dict[int, float] = {}
        for item in orders:
            for op in item.ops:
                lowered = item.lowering.operations.get(id(op))
                if lowered is None or not lowered.candidate_workstations:
                    continue
                share = float(op.expected_time or 0.0) / len(lowered.candidate_workstations)
                for ws_id in lowered.candidate_workstations:
                    loads[ws_id] = loads.get(ws_id, 0.0) + share
        return loads

    def _mtba(self, orders: Sequence[OrderRuntime]) -> float:
        """按全厂平均设备槽位负载与目标利用率反推到达间隔。"""
        loads = self._station_loads(orders)
        if not loads or not orders:
            return 1.0
        # 利用率的分母是**全厂可并行执行的设备槽位**（每工位可能不止一台设备），
        # 所以负载要按槽位摊开再取平均，否则实测值只有设定值的一半。
        slot_loads = [
            loads.get(ws_id, 0.0) / max(1, slots) / len(orders)
            for ws_id, slots in self.workstation_slots.items()
        ]
        mean_per_slot = sum(slot_loads) / max(1, len(slot_loads))
        return max(0.1, mean_per_slot / max(1e-6, self.config.target_utilization))

    # ------------------------------------------------------------------
    def run(self, seed: int) -> SimulationResult:
        started = time.perf_counter()
        self.attribute_records = []
        self._planned = {}
        self._fragments = {}
        config = self.config

        raw_orders = DomainOrderGenerator(
            self.spec, seed=seed, load=config.load).generate(config.orders, self.generator)

        compile_started = time.perf_counter()
        runtimes: List[OrderRuntime] = []
        for index, order in enumerate(raw_orders):
            program = self.generator.generate(order)
            artifacts = self.generator.compile_program(self.proxy, program)["artifacts"]
            compiled = CompiledOrderProgram(
                program=program, cfg=artifacts["cfg"], mdg=artifacts["mdg"],
                op_instr_ids=artifacts["op_instr_ids"],
            )
            ops, op_seqs = _ordered_ops(compiled)
            # 入线搬运送到首道加工工位；不能把新原料停到下游库位阻塞出线。
            for op in ops:
                if op.op_type != "Load":
                    continue
                for vreg in _op_defs(op):
                    consumers = artifacts["lowering"].consuming_capabilities.get(vreg, set())
                    stations = {ws for cap in consumers if not self.f_t.is_logistics_capability(cap)
                                for ws in self.f_t.workstations_supporting(cap)}
                    if stations:
                        artifacts["lowering"].eligible_sites[vreg] = [
                            site for site in artifacts["lowering"].eligible_sites[vreg]
                            if self.f_t.holding_site(site).workstation_id in stations]
            runtimes.append(OrderRuntime(
                order=order, program=program, compiled=compiled,
                lowering=artifacts["lowering"], ops=ops, op_seqs=op_seqs,
                due_time=order.due_time, material_location=None,
                handler=PrecompiledExceptionHandler(
                    artifacts["cfg"], artifacts["exception_table"],
                    artifacts["catch_contingency"]),
            ))
        compile_time = time.perf_counter() - compile_started

        # 到达时刻由目标利用率反推；交期保持原生成器给出的**松弛量**不变
        mtba = self._mtba(runtimes)
        arrival = 0.0
        for item in runtimes:
            slack = (item.order.due_time - item.order.arrival_time) * config.due_slack_factor
            # 指数间隔（泊松到达），均值 = mtba。若直接用 U(0,1)×mtba，
            # 实际平均间隔只有 mtba/2，到达率会凭空翻倍。
            u = min(1.0 - 1e-9, max(1e-9, _rng(seed, len(runtimes), item.order.order_id)))
            arrival += -math.log(u) * mtba
            item.order = _with_arrival(item.order, arrival, arrival + slack)
            item.due_time = item.order.due_time

        ordering = sorted(runtimes, key=lambda item: item.order.arrival_time)
        result = self._simulate(ordering, mtba, seed)
        result.compile_time_s = round(compile_time, 3)
        result.wall_clock_s = round(time.perf_counter() - started, 3)
        return result

    # ------------------------------------------------------------------
    def _simulate(self, runtimes: List[OrderRuntime], mtba: float, seed: int) -> SimulationResult:
        config = self.config
        policy = config.policies[0] if len(config.policies) == 1 else ResponsePolicy.HYBRID
        self.resource_outage_records = []

        events = self._uncertainty_events(runtimes, seed)
        event_cursor = 0

        admitted: List[OrderRuntime] = []
        next_arrival = 0
        station_busy_until: Dict[int, List[float]] = {
            ws: [0.0] * max(1, slots) for ws, slots in self.workstation_slots.items()
        }
        self._station_busy_until = station_busy_until
        agv_free = [0.0] * max(1, _logistics_count(self.proxy.target_model))
        station_busy_total: Dict[int, float] = {ws: 0.0 for ws in station_busy_until}

        self._station_load_hint = {ws: 0.0 for ws in self.workstation_slots}
        self._station_recipes = {}
        workset = None
        allocation: Dict[str, int] = {}
        signature: Optional[Tuple] = None
        self._colorer = WorksetColorer(
            self.f_t, selector=make_site_selector(self.config.site_selector))
        coordinator = UncertaintyCoordinator(
            self.f_t, {}, workset, self._colorer, policy=policy, handlers={}
        )
        coordinator.region_rule = config.region_rule
        coordinator.handlers = {item.compiled.program.order_id: item.handler
                                for item in runtimes if item.handler is not None}

        t = 0.0
        completed = 0
        peak_concurrent = 0
        waiting_peak = 0
        waiting_start: Dict[str, float] = {}
        blocking_total = 0.0
        waiting_total = 0.0
        transport_total = 0.0
        setup_total = 0.0
        colorings = 0
        coloring_time = 0.0
        recompilations = 0
        recompilation_time = 0.0
        rebound = 0
        handler_activations = 0
        blocked_orders = 0
        recovery_busy = 0.0
        rehandled_versions = 0
        revision_origins = {}
        disturbed_revisions = set()
        self.deferred_rehandled_versions = 0
        active_outages = {}
        acknowledged = set()
        released_stations = {}
        self.reassignment_records = []
        self.transport_records = []
        self.transport_requests = {}

        def remember_dispatch_bindings():
            for item in admitted:
                if item.finish_time is not None or item.dispatch_site is not None:
                    continue
                index = item.completed_seq + 1
                site = _output_site(item.ops[index], allocation)
                if site is not None:
                    released_stations.setdefault((item.compiled.program.order_id, index),
                        self.f_t.holding_site(site).workstation_id)

        def report_dispatch_failure(item, kind, target):
            """失效绑定在派工时触发异常；同一指令对同一次停运只报告一次。"""
            pair = (kind, target)
            outage = active_outages[pair]
            index = item.completed_seq + 1
            key = (outage.event_id, item.compiled.program.order_id, index)
            if key in acknowledged:
                return False
            acknowledged.add(key)
            alarm = replace(outage, event_id=f"{outage.event_id}/dispatch/{key[1]}/{index}",
                            time=t, order_id=key[1], op_key=f"{key[1]}:{item.op_seqs[index]}",
                            detail={"source_event": outage.event_id})
            events.insert(event_cursor, alarm)
            return True

        while completed < len(runtimes):
            # --- 1. 订单到达（受 WIP 上限约束） ---
            admitted_open = sum(1 for item in admitted if item.finish_time is None)
            if (next_arrival < len(runtimes)
                    and runtimes[next_arrival].order.arrival_time <= t
                    and admitted_open < config.wip_cap):
                admitted.append(runtimes[next_arrival])
                next_arrival += 1

            # --- 2. 完成事件 ---
            for item in admitted:
                if item.in_transit and item.transport_until <= t + 1e-9:
                    item.in_transit = False
                    item.material_location = item.dispatch_site
                    item.held = {vreg: item.dispatch_site for vreg in item.held}
                if item.executing is None or item.executing_until > t + 1e-9:
                    continue
                station = item.executing_station
                if station is not None:
                    duration = item.executing_until - _op_start(item)
                    station_busy_total[station] = station_busy_total.get(station, 0.0) + duration
                op = item.ops[item.executing]
                item.completed_seq = item.executing
                item.executing = None
                item.dispatch_site = None
                item.held = _held_after(item, op, item.op_site)
                # material_location 是物料占用的**库位**（不是工位号）：
                # 只有拿库位去查 holding_site 才能判断下一道工序是否需要搬运。
                item.material_location = _any_held_site(item.held)
                if item.completed_seq >= len(item.ops) - 1:
                    item.finish_time = item.executing_until
                    item.held.clear()
                    item.material_location = None
                    completed += 1

            self._sync_inventory(admitted, t)
            remember_dispatch_bindings()

            # --- 3. 不确定性事件 ---
            response_batch_started = (time.perf_counter() if self.event_batch_observer and
                event_cursor < len(events) and events[event_cursor].time <= t else None)
            response_observer_time = 0.0
            response_batch_events = 0
            response_batch_outages = 0
            while event_cursor < len(events) and events[event_cursor].time <= t:
                event = events[event_cursor]
                event_cursor += 1
                response_started = time.perf_counter() if self.event_response_observer else None
                fixed_resource = bool(event.detail.get("exogenous_resource")
                                      and "source_event" not in event.detail)
                if fixed_resource:
                    outage_started = time.perf_counter()
                    input_until = event.resolves_at
                    pair = coordinator.detector._availability_pair(event)
                    previous_until = self.f_t.state.unavailable_until.get(pair, 0.0)
                    if input_until is None:
                        raise ValueError("外生资源故障必须给出恢复时刻")
                    # 同资源重复失效只能延长不可用区间，不能令旧故障提前恢复。
                    event = replace(event, resolves_at=max(float(input_until), previous_until))
                    event = self._bind_exogenous_resource_event(event, admitted)
                    outage_record = dict(event_id=event.event_id, time=event.time,
                                         target_kind=event.target_kind, target=event.target,
                                         input_until=input_until, effective_until=event.resolves_at,
                                         order_id=event.order_id, has_owner=event.order_id is not None,
                                         state_apply_time_s=0.0)
                    self.resource_outage_records.append(outage_record)
                    if event.order_id is None:
                        # 空闲或只有已开工工序时仍真实停机；不借用别的订单的处理器。
                        coordinator.detector.apply_state_change(event)
                        active_outages[pair] = event
                        self._apply_production_block(event, station_busy_until, t)
                        outage_record["state_apply_time_s"] = time.perf_counter() - outage_started
                        response_batch_outages += 1
                        if self.resource_outage_observer:
                            observer_started = time.perf_counter()
                            self.resource_outage_observer(event, admitted, dict(allocation))
                            response_observer_time += time.perf_counter() - observer_started
                        continue
                else:
                    event = self._bind_to_live_order(event, admitted)
                if event is None:
                    continue
                target = self._resolve_event_target(event, admitted)
                if target is None:
                    continue
                event = _bind_target(event, target)
                workset = self._compose_workset(admitted)
                allocation = {v: site for v, site in allocation.items()
                              if v in workset.pending_vregs}
                allocation.update(self.f_t.state.wip_location)
                coordinator.set_allocation(allocation)
                coordinator.workset = workset
                coordinator.lowerings = self._lowerings(admitted)
                coordinator.handlers = {item.compiled.program.order_id: self._current_handler(item)
                                        for item in admitted if item.finish_time is None}
                prior_allocation = dict(coordinator.allocation)
                prepare_at_dispatch = self._prepares_alternative_machine(event)
                outcome = coordinator.handle(event)
                if event.type.invalidates_resource and "source_event" not in event.detail:
                    active_outages[coordinator.detector._availability_pair(event)] = event
                    owner = next(item for item in admitted
                                 if item.compiled.program.order_id == event.order_id)
                    acknowledged.add((event.event_id, event.order_id, owner.completed_seq + 1))
                recompilation_time += outcome.recompilation_time_s
                allocation = dict(coordinator.allocation)
                self._planned = dict(allocation)
                attributes = outcome.attributes
                self.attribute_records.append({
                    "kind": event.type.kind.value,
                    "target_kind": event.target_kind or "",
                    "anticipated": bool(attributes.anticipated),
                    "executable_at_trigger": bool(attributes.executable_at_trigger),
                    "contained": bool(attributes.contained),
                    "uncovered_versions": int(attributes.uncovered_versions or 0),
                    "time": round(float(event.time), 3),
                    "source_event": event.detail.get("source_event"),
                })
                if outcome.handler_application is not None:
                    handler_activations += 1
                    # 处置方案是**可执行**的：故障期间能按它改派到替代工位。
                    # 这不是事件挂名那张订单的专利——预编译方案写在每一张订单程序里，
                    # 凡是"已预料到这类故障、且下一道工序正等着这台停机工位"的订单，
                    # 此刻都拿得出改派方案。只让挂名订单享受，等于把预定义的价值
                    # 缩到一张订单上。
                    until = float(event.resolves_at or t)
                    failed = event.target if event.target_kind == TARGET_WORKSTATION else None
                    for item in admitted:
                        if item.finish_time is not None:
                            continue
                        if item.compiled.program.order_id == event.order_id:
                            item.reroute_until = max(item.reroute_until, until)
                            item.fixed_handler_binding = outcome.handler_application.binding
                            item.fixed_handler_locations.update(outcome.handler_application.binding.allocation)
                            continue
                        if failed is None or item.handler is None:
                            continue
                        if not self._current_handler(item).handles(event.type.exception_type):
                            continue
                        index = item.completed_seq + 1
                        if index >= len(item.ops):
                            continue
                        site = _output_site(item.ops[index], allocation)
                        if site is not None and self.f_t.holding_site(site).workstation_id == failed:
                            item.reroute_until = max(item.reroute_until, until)
                            item.fixed_handler_binding = self._current_handler(item).binding_for(event)
                    if prepare_at_dispatch:
                        busy, recovery_finish = 0.0, t
                    else:
                        busy, recovery_finish = self._charge_recovery_resources(
                            coordinator, event, outcome, t,
                            station_busy_until, station_busy_total, agv_free)
                    recovery_busy += busy
                    for item in admitted:
                        if item.compiled.program.order_id == event.order_id:
                            item.blocked_until = max(item.blocked_until, recovery_finish)
                if outcome.recompile is not None and outcome.recompile.rebuilt:
                    recompilations += 1
                    rebound += len(outcome.recompile.region.expansion)
                    # 建议2：重规划的代价用**车间量**计，不用 CPU 时间。
                    # 落位被改判 ⇒ 现场要重新下达作业指令、重新搬运物料；
                    # 卷入的工单 ⇒ 在制品流转被打断。两者都直接推高运营成本。
                    for order_id, versions in outcome.recompile.region.by_order.items():
                        for vreg in versions:
                            old_site = prior_allocation.get(vreg)
                            if isinstance(old_site, int):
                                revision_origins.setdefault(vreg, (event.event_id, order_id, old_site))
                    rehandled_versions += self._settle_rebindings(
                        allocation, revision_origins, disturbed_revisions)
                # 未被处置的异常必须在生产上留下后果，否则"跳过处置"会因为省下产能
                # 而显得更优。必须放在下面的处置分支**之前**：Reactive 恒走"整区重编译"
                # 分支，放在 elif 链里它永远到不了。
                if not outcome.plan.use_handler:
                    if _is_latent_defect(event):
                        # 已经做出来的东西有缺陷：留在制品上，检验时显形并返工
                        for item in admitted:
                            if item.compiled.program.order_id == event.order_id:
                                item.pending_defects.append(event)
                    elif _is_blocking_shortage(event):
                        # 料没到：无论走哪条路径，生产都推不动，必须等补料。
                        # 等待时长与"当场处置"**等价计价**（同一套取料 + 补料动作之和）：
                        # 料不会自己走到线边，取料这件事必须有人做，跳过处置并不能省掉它。
                        for item in admitted:
                            if item.compiled.program.order_id != event.order_id:
                                continue
                            # 等补料的同时，"去取料、去补料"这件事本身要占用设备：
                            # 料不会自己走到线边。只让订单等待而不占用任何资源，
                            # 等于让"不处置"白省下一批设备的占用时间。
                            busy, until = self._charge_exception_plan(
                                coordinator, event, item.handler,
                                self._catch_block_id_of(item, event.type.exception_type),
                                t, station_busy_until, station_busy_total, agv_free)
                            recovery_busy += busy
                            item.blocked_until = max(item.blocked_until, until)

                if outcome.plan.recompile and outcome.recompile is not None:
                    # 改判落位由后续派工使用，搬运时间在实际发生时结算。
                    blocked_orders += 1
                elif outcome.handler_material_waiting:
                    # 处理器被启用了，但它的物料此刻落不下去——处置动作起不来。
                    # 这是 executability at trigger 在生产上的直接后果。
                    # 等待时长不设常数：订单一直卡到这些版本真的被落下为止，
                    # 而"能不能落下"由资源本身（工位可用性、库位余量）决定。
                    blocked_orders += 1
                    stuck = set(outcome.handler_material_waiting)
                    # 物料落不下去，是因为处置动作要用的资源此刻拿不到。
                    # 那就得**等到资源恢复**才能把这件事做掉——只等"找到一个空位"
                    # 会把这个代价缩小到几乎为零，等于把预定义策略最脆弱的一面藏起来。
                    until = float(event.resolves_at or t)
                    for item in admitted:
                        if _owns_any(item, stuck):
                            item.blocked_handler_vregs |= stuck
                        if item.compiled.program.order_id == event.order_id:
                            item.blocked_until = max(item.blocked_until, until)
                elif outcome.plan.use_handler and not attributes.executable_at_trigger:
                    # 处理器写在程序里，但它的动作集此刻拿不到物理资源——它跑不起来。
                    # 仍然启用它的策略（Predefinition 没有退路）并不能让处置发生，
                    # 只能等资源恢复。让"启用一个跑不起来的处理器"毫无代价，
                    # 等于把预定义策略最脆弱的那一面藏了起来。
                    blocked_orders += 1
                    until = float(event.resolves_at or t)
                    for item in admitted:
                        if item.compiled.program.order_id == event.order_id:
                            item.blocked_until = max(item.blocked_until, until)
                elif outcome.plan.use_handler and not attributes.contained:
                    # 预定义兜不住、又不重编译：候选集被这次事件作废的版本**无处可去**，
                    # 只能一直等到资源恢复。这才是"预定义兜不住"的真实代价，而且它落在
                    # 所有持有这些版本的订单上，不只是事件挂名的那一张。
                    uncovered = coordinator.detector.versions_with_invalidated_candidates(
                        event, coordinator.workset, event.time,
                        exclude=attributes_handler_vregs(outcome.handler_application),
                        allocation=coordinator.allocation)
                    if uncovered:
                        until = float(event.resolves_at or t)
                        blocked_orders += 1
                        for item in admitted:
                            if _owns_any(item, uncovered):
                                item.blocked_until = max(item.blocked_until, until)
                elif not attributes.anticipated and not outcome.plan.recompile:
                    # 既没有预定义处理器、又不重编译：这次事件在生产上**没有任何处置**。
                    # 这正是"预定义"的边界——它只能覆盖生成订单程序时预料到的情形；
                    # 没预料到的事件，预定义策略手里没有牌可打，受影响的订单只能
                    # 干等事件自行消解。
                    blocked_orders += 1
                    until = float(event.resolves_at or t)
                    for item in admitted:
                        if item.compiled.program.order_id == event.order_id:
                            item.blocked_until = max(item.blocked_until, until)
                self._apply_production_block(event, station_busy_until, t)
                response_batch_events += 1
                if self.event_response_observer:
                    # 到即时分配、恢复资源预约、阻塞状态写回完成为止；不含诊断与磁盘写入。
                    response_elapsed = time.perf_counter() - response_started
                    observer_started = time.perf_counter()
                    self.event_response_observer(
                        event, outcome, coordinator, prior_allocation, response_elapsed)
                    response_observer_time += time.perf_counter() - observer_started

            # --- 4. 给新增及 WAIT 版本落位，沿用其余版本的计划 ---
            current_signature = (_workset_signature(admitted),
                                 _availability_signature(self.f_t, t))
            if workset is None or current_signature != signature:
                color_started = time.perf_counter()
                workset, lowerings, allocation, waiting = self._recolor(admitted, t)
                deferred = self._settle_rebindings(allocation, revision_origins, disturbed_revisions)
                rehandled_versions += deferred
                self.deferred_rehandled_versions += deferred
                coloring_time += time.perf_counter() - color_started
                signature = current_signature
                colorings += 1
                coordinator.workset = workset
                coordinator.lowerings = lowerings
                coordinator.set_allocation(allocation)
                waiting_now = {v for v in waiting}
                waiting_peak = max(waiting_peak, len(waiting))
                now = t
                for vreg in waiting:
                    waiting_start.setdefault(vreg, now)
                for vreg in list(waiting_start):
                    if vreg not in waiting:
                        blocking_total += now - waiting_start.pop(vreg)

            peak_concurrent = max(peak_concurrent, sum(
                1 for item in admitted if item.finish_time is None))

            # --- 5. 派工 ---
            for item in admitted:
                item.blocked_handler_vregs = {
                    v for v in item.blocked_handler_vregs
                    if not coordinator._fixed_site_available(v, item.fixed_handler_locations[v], t)}
            remember_dispatch_bindings()
            progressed = False
            agv_blocked_requests = set()
            for item in admitted:
                if item.finish_time is not None or item.executing is not None:
                    item.waiting_since = None
                    continue
                if item.completed_seq + 1 >= len(item.ops):
                    item.waiting_since = None
                    continue
                # 走到这里说明该订单的下一道工序还没开工——无论出于什么原因
                # （在途、被冻结、工位不可用、槽位占满、没有 AGV），都算作等待。
                # 只记起点，不在这里累加。
                if item.waiting_since is None:
                    item.waiting_since = t
                if item.transport_until > t + 1e-9:
                    continue
                if item.blocked_until > t + 1e-9:
                    continue
                if item.blocked_handler_vregs:
                    # 处置动作起不来，这道工序只能等
                    continue
                op = item.ops[item.completed_seq + 1]
                if item.dispatch_site is None and any(
                        vreg not in allocation for vreg in _op_defs(op)):
                    continue
                dest_site = (item.dispatch_site if item.dispatch_site is not None
                             else _output_site(op, allocation))
                # 已发车/开工的物理落点是派工依据；局部着色会改写 IR 上的临时绑定。
                station = (self.f_t.holding_site(dest_site).workstation_id
                           if dest_site is not None else getattr(op, "assigned_workstation_id", None))
                if station is None:
                    continue
                if not self.f_t.is_available(AVAIL_WORKSTATION, station, t):
                    # 原定工位不可用。有预编译处置方案且仍在有效期内的订单，
                    # 按方案改派到具备同一能力的替代工位；没有方案的只能等。
                    if item.dispatch_site is None and t + 1e-9 < item.reroute_until:
                        alternative = self._alternative_site(item, op, station, t)
                        if alternative is not None:
                            dest_site = alternative
                            station = self.f_t.holding_site(dest_site).workstation_id
                            for vreg in _op_defs(op):
                                allocation[vreg] = dest_site
                                self._planned[vreg] = dest_site
                                coordinator.allocation[vreg] = dest_site
                    if not self.f_t.is_available(AVAIL_WORKSTATION, station, t):
                        progressed |= report_dispatch_failure(item, AVAIL_WORKSTATION, station)
                        continue
                slots = station_busy_until.get(station)
                if slots:
                    self._station_load_hint[station] = max(slots)
                if not slots:
                    continue
                free_index = next((i for i, free in enumerate(slots) if free <= t + 1e-9), None)
                if free_index is None:
                    continue

                if dest_site is not None and not self.f_t.is_site_available(
                        dest_site, t, source_site=item.material_location):
                    if not self.f_t.is_available(AVAIL_HOLDING, dest_site, t):
                        progressed |= report_dispatch_failure(item, AVAIL_HOLDING, dest_site)
                    else:
                        progressed |= report_dispatch_failure(
                            item, AVAIL_LOGISTICS, self.f_t.logistics_resource().palu_id)
                    continue
                if dest_site is not None and dest_site != item.material_location:
                    if self.f_t.occupancy_of(dest_site) >= self.f_t.capacity_of(dest_site):
                        continue
                if self.dispatch_uncertainty_factory and item.dispatch_site is None:
                    alarm = self.dispatch_uncertainty_factory(item, op, station, t, seed)
                    if alarm is not None:
                        events.insert(event_cursor, alarm)
                        progressed = True
                        break  # 先处理启动报警，再允许本工序搬运/加工；仿真时间不前进。
                dispatch_key = (item.compiled.program.order_id, item.completed_seq + 1)
                previous_station = released_stations.get(dispatch_key, station)
                if (self.reassignment_actions and op.op_type in {"Process", "Inspect", "Adjust"}
                        and station != previous_station):
                    # 已下达工序换机时，执行工艺规定的人员交接；故障种类与策略不改变动作。
                    fixed = item.fixed_handler_binding
                    if (fixed is not None and
                            (fixed.exception_type != "MachineFailure" or
                             any(fixed.allocation.get(v) != dest_site for v in _op_defs(op)))):
                        fixed = None
                    busy, ready = self._charge_reassignment(
                        t, station_busy_until, station_busy_total, agv_free, fixed)
                    recovery_busy += busy
                    item.blocked_until = ready
                    released_stations[dispatch_key] = station
                    self.reassignment_records.append((dispatch_key, previous_station, station, busy))
                    continue
                dest_ws = None
                if dest_site is not None:
                    dest = self.f_t.holding_site(dest_site)
                    dest_ws = dest.workstation_id if dest is not None else None

                # 物料在别的工位上，就得先搬过来：搬运是**独立的一段占用时间**，
                # 由 AGV 执行，在途期间该订单不能开工。把搬运折进加工时长会
                # 让"等搬运"和"在加工"混为一谈，也让作业排队无处体现。
                if dest_ws is not None and item.material_location is not None:
                    src = self.f_t.holding_site(item.material_location)
                    if src is not None and src.workstation_id != dest_ws:
                        request = None
                        if self.collect_transport_records:
                            request = self.transport_requests.setdefault(dispatch_key, dict(
                                order_id=item.compiled.program.order_id,
                                op_key=f"{item.compiled.program.order_id}:{item.op_seqs[item.completed_seq + 1]}",
                                requested=t, agv_wait=0.0, departed=False))
                        agv_index = next((i for i, free in enumerate(agv_free)
                                          if free <= t + 1e-9), None)
                        if agv_index is None:
                            if request is not None:
                                agv_blocked_requests.add(dispatch_key)
                            continue
                        transit = self._transport_time(item.material_location, dest_ws)
                        if request is not None:
                            a, b = self.ws_coords.get(src.workstation_id), self.ws_coords.get(dest_ws)
                            distance = abs(a[0] - b[0]) + abs(a[1] - b[1]) if a and b else None
                            request["departed"] = True
                            self.transport_records.append(dict(
                                order_id=item.compiled.program.order_id,
                                op_key=request["op_key"], from_site=item.material_location,
                                from_ws=src.workstation_id, to_site=dest_site, to_ws=dest_ws,
                                requested=request["requested"], departure=t, arrival=t + transit,
                                duration=transit, agv_wait=request["agv_wait"],
                                distance_steps=distance, agv_index=agv_index))
                        agv_free[agv_index] = t + transit
                        item.transport_until = t + transit
                        item.in_transit = True
                        item.dispatch_site = dest_site
                        item.material_location = None
                        transport_total += transit
                        self._sync_inventory(admitted, t)
                        continue

                # 检验或出线检查发现未消解的缺陷，执行与即时处理相同的实体恢复动作。
                last_op = item.completed_seq + 1 >= len(item.ops) - 1
                if item.pending_defects and (op.op_type == "Inspect" or last_op):
                    ready = t
                    for defect in item.pending_defects:
                        busy, ready = self._charge_exception_plan(
                            coordinator, defect, item.handler,
                            self._catch_block_id_of(item, defect.type.exception_type), ready,
                            station_busy_until, station_busy_total, agv_free)
                        recovery_busy += busy
                    item.pending_defects.clear()
                    item.blocked_until = ready
                    continue
                start = t
                duration = _op_duration(op, self.config, seed, item.order.order_id, item.completed_seq + 1)
                setup = self._charge_setup(op, station, free_index)
                setup_total += setup
                duration += setup
                finish = start + duration
                slots[free_index] = finish
                item.op_site = dest_site
                item.dispatch_site = item.op_site
                if item.op_site is not None:
                    item.material_location = item.op_site
                    item.held = {vreg: item.op_site for vreg in item.held}
                item.executing = item.completed_seq + 1
                item.executing_until = finish
                item.executing_station = station
                item.op_start = start
                self._sync_inventory(admitted, t)
                if item.waiting_since is not None:
                    waiting_total += max(0.0, start - item.waiting_since)
                    item.waiting_since = None
                if item.start_time is None:
                    item.start_time = start
                progressed = True

            if response_batch_started is not None and (response_batch_events or response_batch_outages):
                # 同批事件共用后续着色/派工，完整计时只记一次，不重复分摊给各事件。
                self.event_batch_observer(t, response_batch_events,
                    time.perf_counter() - response_batch_started - response_observer_time)

            if progressed:
                continue

            # 时钟只能跳到"下一件真正会发生的事"：在制工序完工、在途运输到达、新订单
            # 到达（且 WIP 上限允许）、不确定性事件触发，或某个被占/被停的资源释放。
            # 绝不允许靠微小增量往前蹭——那样一旦有资源卡住，时钟会空转而不是报错。
            next_times: List[float] = []
            next_times.extend(
                item.transport_until for item in admitted
                if item.finish_time is None and item.transport_until > t + 1e-9)
            next_times.extend(
                item.executing_until for item in admitted
                if item.executing is not None and item.executing_until > t + 1e-9)
            if (next_arrival < len(runtimes)
                    and admitted_open < config.wip_cap
                    and runtimes[next_arrival].order.arrival_time > t + 1e-9):
                next_times.append(runtimes[next_arrival].order.arrival_time)
            next_times.extend(
                ev.time for ev in events[event_cursor:] if ev.time > t + 1e-9)
            next_times.extend(
                free for slots in station_busy_until.values()
                for free in slots if free > t + 1e-9)
            next_times.extend(free for free in agv_free if free > t + 1e-9)
            next_times.extend(
                until for until in self.f_t.state.unavailable_until.values()
                if until > t + 1e-9)
            next_times.extend(
                item.blocked_until for item in admitted if item.blocked_until > t + 1e-9)

            if completed >= len(runtimes):
                break

            step = min(next_times) if next_times else float("inf")
            if step == float("inf") or step <= t + 1e-9:
                # 无任何可推进的事件，说明有资源把在制品永久卡住了——直接报错，
                # 不要靠 +0.5 空转把问题埋掉。
                blocked = []
                for item in admitted:
                    if item.finish_time is not None:
                        continue
                    index = item.completed_seq + 1
                    op = item.ops[index] if index < len(item.ops) else None
                    blocked.append(
                        f"{item.order.order_id}[{index}/{len(item.ops)}]"
                        f" op={op.op_type if op else 'done'}"
                        f" ws={getattr(op, 'assigned_workstation_id', None) if op else None}"
                        f" matloc={item.material_location}")
                raise RuntimeError(
                    f"deadlock at t={t:.1f}: 无可推进事件；"
                    f"completed={completed}/{len(runtimes)} admitted={len(admitted)} "
                    + "; ".join(blocked[:6]))
            # 只累计本轮其他派工条件均通过、实际卡在AGV门槛的区间。
            # 若有别的资源/事件阻塞，下一轮不会进入此集合，不混记为AGV等待。
            if self.collect_transport_records:
                for key in agv_blocked_requests:
                    self.transport_requests[key]["agv_wait"] += step - t
            t = step

        first_arrival = min(item.order.arrival_time for item in runtimes)
        last_arrival = max(item.order.arrival_time for item in runtimes)
        horizon = max(1.0, max(item.finish_time for item in runtimes) - first_arrival)
        total_slots = sum(max(1, s) for s in self.workstation_slots.values())

        # 资源利用率按 **makespan 窗口**计：分母是设备槽位数 × (末单完工 − 首单到达)。
        # 这与原 FaLang case study 的口径一致（simulation.py: horizon = finish - start,
        # resource_capacity_time = Σ slots × horizon）。投料区间口径会让该指标对
        # "总工作量相同"的任何策略恒等，只能描述场景负荷，不能用于方法比较。
        utilization = sum(station_busy_total.values()) / (total_slots * horizon)
        feeding_window = max(1e-6, last_arrival - first_arrival)
        feeding_utilization = sum(station_busy_total.values()) / (total_slots * feeding_window)

        return SimulationResult(
            domain=self.config.domain,
            policy=policy.value,
            seed=seed,
            load=self.config.load,
            orders=len(runtimes),
            orders_per_hour=round(60.0 / mtba, 2),
            concurrent_peak=peak_concurrent,
            makespan=round(horizon, 2),
            mean_flow_time=round(sum(item.finish_time - item.order.arrival_time
                                     for item in runtimes) / len(runtimes), 2),
            mean_tardiness=round(sum(max(0.0, item.finish_time - item.due_time)
                                     for item in runtimes) / len(runtimes), 2),
            on_time_delivery_rate=round(
                sum(1 for item in runtimes if item.finish_time <= item.due_time) / len(runtimes), 4),
            resource_utilization=round(utilization, 4),
            feeding_window_utilization=round(feeding_utilization, 4),
            recovery_busy_time=round(recovery_busy, 2),
            rehandled_versions=rehandled_versions,
            disturbed_orders=len(disturbed_revisions),
            blocking_time=round(blocking_total, 2),
            waiting_time=round(waiting_total, 2),
            transport_time=round(transport_total, 2),
            setup_time=round(setup_total, 2),
            uncertainty_events=sum(r["source_event"] is None for r in self.attribute_records),
            dispatch_repair_requests=sum(r["source_event"] is not None for r in self.attribute_records),
            handler_activations=handler_activations,
            recompilations=recompilations,
            recompilation_time_s=round(recompilation_time, 8),
            rebound_versions=rebound,
            blocked_orders=blocked_orders,
            waiting_versions_peak=waiting_peak,
            colorings=colorings,
            coloring_time_s=round(coloring_time, 4),
            compile_time_s=0.0,
            wall_clock_s=0.0,
        )
    @staticmethod
    def _settle_rebindings(allocation, origins, disturbed):
        """WAIT 保留改判前的库位；重获绑定后与旧计划比较，同事件同订单只计一次。"""
        changed = 0
        for vreg in list(origins):
            if vreg not in allocation:
                continue
            event_id, order_id, previous = origins.pop(vreg)
            if allocation[vreg] != previous:
                changed += 1
                disturbed.add((event_id, order_id))
        return changed

    def _prepares_alternative_machine(self, event):
        return event.type.key == "machine_failure" and any(
            rule.exception_type == "MachineFailure" and
            rule.recovery == (("Adjust", "Recovery_PM", "Operator_Reassign"),)
            for rule in self.spec.protections)

    def _charge_setup(self, op, station, slot):
        """实体执行槽位按组件族记住上次型号，换型占用同一设备。"""
        params = self.proxy.target_model.parse_operation_params(op.params)
        family = params.get("component")
        if not family or not params.get("model"):
            return 0.0
        key = (station, slot, family)
        previous = self._station_recipes.get(key)
        duration = self.proxy.target_model.get_setup_time(previous, op.params)
        self._station_recipes[key] = op.params
        return duration

    def _sync_inventory(self, admitted: Sequence[OrderRuntime], t: float):
        """现场持有与已发车/开工的目的库位，共用一份容量账。"""
        occupied = Counter()
        locations = {}
        for item in admitted:
            if item.finish_time is not None:
                continue
            site = item.dispatch_site if item.dispatch_site is not None else item.material_location
            if site is None:
                continue
            occupied[site] += 1
            locations.update({vreg: site for vreg in item.held})
            if item.dispatch_site is not None:
                locations.update({vreg: site for vreg in _next_output_versions(item)})
        self.f_t.state.time = t
        self.f_t.state.occupied = dict(occupied)
        self.f_t.state.wip_location = locations

    def _compose_workset(self, admitted: Sequence[OrderRuntime]):
        active = [item for item in admitted if item.finish_time is None]
        workset = ResidualWorkset()
        for item in active:
            order_id = item.compiled.program.order_id
            key = (item.completed_seq, item.dispatch_site is not None)
            cached = self._fragments.get(order_id)
            if cached is None or cached[0] != key:
                record = ResidualRecord(
                    order_id=order_id, completed_seq=_logical_seq(item, item.completed_seq),
                    executing_seqs=(item.op_seqs[item.completed_seq + 1],)
                    if item.dispatch_site is not None else ())
                fragment = WorksetComposer().compose(
                    [item.compiled], {order_id: record}, self.f_t.state).fragments[0]
                # Move/Assign 由物理搬运与版本交接执行，不另占生产库位。
                remaining = {v for op in item.ops[item.completed_seq + 1:] for v in _op_defs(op)}
                fragment.pending_vregs &= remaining
                fragment.carried_vregs &= remaining
                self._fragments[order_id] = (key, fragment)
            else:
                fragment = cached[1]
            workset.fragments.append(fragment)
            workset.pending_vregs.update(fragment.pending_vregs)
            workset.carried_vregs.update(fragment.carried_vregs)
            workset.frozen_vregs.update(fragment.frozen_vregs)
            ready_at = max(self.f_t.state.time, item.blocked_until, item.transport_until)
            for index in range(item.completed_seq + 1, len(item.ops)):
                op = item.ops[index]
                site = _output_site(op, self._planned)
                if site is not None:
                    station = self.f_t.holding_site(site).workstation_id
                    slots = self._station_busy_until.get(station)
                    if slots:
                        ready_at = max(ready_at, min(slots))
                workset.earliest_use.update({v: ready_at for v in _op_defs(op)})
                if index == item.executing:
                    ready_at = max(ready_at, item.executing_until)
                else:
                    # 只用已知的时长下界，不读取将来加工扰动、排队或故障结果。
                    ready_at += max(0.1, float(op.expected_time or 1.0)
                                    * (1.0 - self.config.processing_cv))
        workset.pinned_locations = {v: site for v, site in self.f_t.state.wip_location.items()
                                   if v in workset.carried_vregs}
        return workset

    def _recolor(self, admitted: Sequence[OrderRuntime], t: float):
        workset = self._compose_workset(admitted)
        lowerings = self._lowerings(admitted)
        # 例行着色保留既有计划；事件改判已在 coordinator.handle 后写入。
        pinned = {vreg: site for vreg, site in self._planned.items()
                  if vreg in workset.pending_vregs}
        # 现场位置和已发车的目的地优先于未执行的计划。
        pinned.update(self.f_t.state.wip_location)
        ready = {v for item in admitted for v in _next_output_versions(item)}
        # 远期工序保留选址计划；当前派工只为就绪工序预留容量。
        current = {v: site for v, site in pinned.items()
                   if v in ready or v in self.f_t.state.wip_location}
        affected = {
            item.compiled.program.order_id: (
                [] if item.dispatch_site is not None else
                [v for v in _next_output_versions(item) if v not in pinned])
            for item in admitted
        }
        result = self._colorer.color(workset.fragments, lowerings, current, time=t,
                                     affected_vregs=affected)
        pinned.update(result.allocation)
        self._planned = pinned
        return workset, lowerings, dict(pinned), result.waiting

    def _lowerings(self, admitted: Sequence[OrderRuntime]) -> Dict[str, object]:
        return {
            item.compiled.program.order_id: item.lowering
            for item in admitted
        }

    # ------------------------------------------------------------------
    def _uncertainty_events(self, runtimes: Sequence[OrderRuntime],
                            seed: int) -> List[UncertaintyEvent]:
        config = self.config
        # 总事件数由"资源态事件强度"反推：资源态事件在总事件中的占比是
        # (1 - group_a_share)。B 的占比为 1、C 为 1/4，因此同一个
        # resource_events_per_order 会让两者的资源态负荷相同，总事件数不同。
        resource_share = 1.0 - config.group_a_share
        if resource_share > 0.0:
            count = int(round(
                config.resource_events_per_order * len(runtimes) / resource_share))
        else:
            count = int(round(config.events_per_order * len(runtimes)))
        span_start = min(item.order.arrival_time for item in runtimes)
        span_end = max(item.order.due_time for item in runtimes)
        stream = UncertaintyStream(
            seed=seed, group_a_share=config.group_a_share,
            group_a=GROUP_A,
            group_b=tuple(kind for kind, weight in zip(GROUP_B, config.resource_event_weights)
                          for _ in range(weight)),
        )
        events = stream.generate(count, (span_start, span_end))
        resolved = []
        for index, event in enumerate(events):
            # 恢复时长按事件**种类**取，不能一律套设备修复时长：
            # 缺料不会"修好"，它是靠补料消解的；质量偏差靠返工消解。
            # 前者用设备修复时长，后者用该不确定性的预定义恢复动作本身要花的时间，
            # 全部取自工艺规格，不引入任何常数。
            if event.type.invalidates_resource:
                delay = config.repair_delay[0] + (
                    config.repair_delay[1] - config.repair_delay[0]
                ) * _rng(seed, index, 7)
            else:
                delay = self._recovery_duration(event)
            resolved.append(UncertaintyEvent(
                event_id=event.event_id, type=event.type, time=event.time,
                order_id=event.order_id, op_key=event.op_key,
                target_kind=event.target_kind, target=event.target,
                resolves_at=event.time + delay,
            ))
        return resolved

    def _bind_to_live_order(self, event: UncertaintyEvent,
                            admitted: Sequence[OrderRuntime]):
        """
        把事件挂到一张**在制**订单上。

        事件流给出的订单号可能压根不在场（资源态事件挂的是工位而非订单）。
        挂到不在场的订单上，这次事件对生产不产生任何作用——既不会有订单因此停工，
        也不会有缺陷被记上，等于空放。

        Scenario A 另有更严的要求：必须挂到"程序里确有该处理器"的订单上，
        使 anticipation 恒为真（方案 §10.2）。没有合适的在制订单时放弃该事件。
        """
        if "source_event" in event.detail:
            return event
        if event.detail.get("exogenous_resource"):
            return self._bind_exogenous_resource_event(event, admitted)
        exception_type = event.type.exception_type
        live = [
            item for item in admitted
            if item.finish_time is None
            and (not self.config.anticipated_only
                 or (exception_type and self._current_handler(item).handles(exception_type)))
        ]
        if exception_type in self.spec.catch_triggers:
            live = [item for item in live if self.proxy.target_model.triggers_exception(
                exception_type, item.ops[item.completed_seq + 1].opcode,
                item.ops[item.completed_seq + 1].func, item.ops[item.completed_seq + 1].rp)]
        if event.type.invalidates_resource and self.config.resource_events_at_dispatch:
            live = [item for item in live if item.dispatch_site is None and
                    _output_site(item.ops[item.completed_seq + 1], self._planned) is not None]
        if not live:
            return None
        chosen = next((item for item in live if item.compiled.program.order_id == event.order_id),
                      live[int(event.time * 1000) % len(live)])
        return replace(event, order_id=chosen.compiled.program.order_id,
                       op_key=f"{chosen.compiled.program.order_id}:{chosen.op_seqs[chosen.completed_seq + 1]}",
                       detail={**event.detail, "interrupted": chosen.executing is not None})

    def _bind_exogenous_resource_event(self, event: UncertaintyEvent,
                                       admitted: Sequence[OrderRuntime]):
        """只由确实等待固定故障工位且能触发异常的未开工工序接警。"""
        if event.target_kind != TARGET_WORKSTATION:
            raise ValueError("外生固定资源日历目前仅支持工位故障")
        if event.target not in self.workstation_slots:
            raise ValueError(f"未知外生故障工位: {event.target}")
        eligible = []
        for item in admitted:
            if (item.finish_time is not None or item.executing is not None
                    or item.in_transit or item.dispatch_site is not None):
                continue
            index = item.completed_seq + 1
            if index >= len(item.ops):
                continue
            op = item.ops[index]
            site = _output_site(op, self._planned)
            if (site is None or self.f_t.holding_site(site).workstation_id != event.target
                    or not self.proxy.target_model.triggers_exception(
                        event.type.exception_type, op.opcode, op.func, op.rp)):
                continue
            eligible.append(item)
        if not eligible:
            return replace(event, order_id=None, op_key=None)
        chosen = eligible[int(event.time * 1000) % len(eligible)]
        return replace(event, order_id=chosen.compiled.program.order_id,
                       op_key=f"{chosen.compiled.program.order_id}:{chosen.op_seqs[chosen.completed_seq + 1]}",
                       detail={**event.detail, "interrupted": False})

    @staticmethod
    def _current_handler(item):
        return item.handler.for_instruction(item.ops[item.completed_seq + 1])

    def _resolve_event_target(self, event: UncertaintyEvent, admitted: Sequence[OrderRuntime]):
        if "source_event" in event.detail or event.detail.get("exogenous_resource"):
            return event.target
        owner = next(item for item in admitted if item.compiled.program.order_id == event.order_id)
        op = owner.ops[owner.completed_seq + 1]
        site = (owner.dispatch_site if owner.dispatch_site is not None
                else _output_site(op, self._planned))
        if event.type.target_kind == TARGET_WORKSTATION:
            # 报警工单就是当前使用/等待故障设备的工单，event_order 基线修复它。
            if site is not None:
                return self.f_t.holding_site(site).workstation_id
            stations = owner.lowering.operations[id(op)].candidate_workstations
            return stations[int(event.time * 1000) % len(stations)]
        if event.type.target_kind == TARGET_HOLDING:
            return site if site is not None else owner.material_location
        if event.type.target_kind == TARGET_LOGISTICS:
            logistics = self.f_t.logistics_resource()
            return logistics.palu_id if logistics else None
        if event.type.target_kind == "material":
            unfinished = [item for item in admitted if item.finish_time is None]
            if event.order_id:
                # 事件已经按"该订单程序里确有这个处理器"绑定过了，
                # 这里再随机换一张订单会把那次绑定抹掉；但已完工的订单不该再接事件。
                if any(item.compiled.program.order_id == event.order_id
                       for item in unfinished):
                    return event.order_id
                if not unfinished:
                    return None
            pending = [item for item in admitted if item.finish_time is None]
            if not pending:
                return None
            chosen = pending[int(event.time) % len(pending)]
            return chosen.compiled.program.order_id
        return None

    def _alternative_site(self, item: OrderRuntime, op: OpInstr,
                          failed: int, t: float) -> Optional[int]:
        """只读取首次编译的替代绑定；不可用时遵循原来的等待规则。"""
        outputs = _op_defs(op)
        if not outputs:
            return None
        binding = item.fixed_handler_binding
        if binding is None or any(v not in binding.allocation for v in outputs):
            return None
        site = binding.allocation[outputs[0]]
        if (self.f_t.holding_site(site).workstation_id == failed or
                not self.f_t.is_site_available(site, t, source_site=item.material_location) or
                self.f_t.capacity_of(site) - self.f_t.occupancy_of(site) + int(item.material_location == site) < 1):
            return None
        return site

    @staticmethod
    def _catch_block_id_of(item: "OrderRuntime", exception_type: str):
        handler = getattr(item, "handler", None)
        if handler is None:
            return None
        for entry in handler.exception_table:
            if getattr(entry, "exception_type", None) != exception_type:
                continue
            block = getattr(entry, "catch_block", None)
            if block is not None:
                return getattr(block, "id", None)
        return None

    def _charge_recovery_resources(self, coordinator, event, outcome, t,
                                   station_busy_until, station_busy_total, agv_free):
        application = outcome.handler_application
        instructions = coordinator.handler_for(event).recovery_instructions(
            event.type.exception_type, application.catch_block_id)
        return self._execute_fixed_recovery_actions(instructions, application.binding, t,
                                                    station_busy_until, station_busy_total, agv_free)

    def _execute_fixed_recovery_actions(self, instructions, binding, t,
                                        station_busy_until, station_busy_total, agv_free):
        charged, ready = 0.0, t
        for instr in instructions:
            duration = float(instr.expected_time or 0.0)
            if duration <= 0.0:
                continue
            kind, resource, index = binding.actions[id(instr)]
            if kind == 'logistics':
                from compiler.factory_target_machine import AVAIL_LOGISTICS
                available = self.f_t.state.unavailable_until.get((AVAIL_LOGISTICS, resource), 0.0)
                ready = max(ready, agv_free[index], available) + duration
                agv_free[index] = ready
            else:
                available = self.f_t.state.unavailable_until.get((AVAIL_WORKSTATION, resource), 0.0)
                ready = max(ready, station_busy_until[resource][index], available) + duration
                station_busy_until[resource][index] = ready
                station_busy_total[resource] += duration
            charged += duration
        return charged, ready

    def _charge_exception_plan(self, coordinator, event, handler, catch_block_id, t,
                               station_busy_until, station_busy_total, agv_free):
        """顺序执行恢复动作；物流占用 AGV，生产动作占用对应工位。"""
        from compiler.factory_target_machine import AVAIL_LOGISTICS

        lowering = coordinator.lowerings[event.order_id]
        if catch_block_id is None:
            # 未预定义补料处理器的订单仍需执行工艺规定的补料动作。
            rule = next(r for r in self.spec.protections if r.exception_type == event.type.exception_type)
            recovery = ControlFlowGraph()
            for op_type, role, description in rule.recovery:
                recovery.entry_block.add_instr(OpInstr(
                    op_type, None, [role] if role else [], params=description,
                    expected_time=self.spec.action_time(op_type, description)))
            lowering = TargetLowering(self.f_t).lower(recovery)
            instructions = recovery.entry_block.instructions
        else:
            instructions = handler.recovery_instructions(event.type.exception_type, catch_block_id)
        return self._execute_recovery_actions(instructions, lowering, t,
                                              station_busy_until, station_busy_total, agv_free)

    def _charge_reassignment(self, t, station_busy_until, station_busy_total, agv_free, binding=None):
        if binding is not None:
            return self._execute_fixed_recovery_actions(binding.instructions, binding, t,
                                                        station_busy_until, station_busy_total, agv_free)
        recovery = ControlFlowGraph()
        for op_type, role, description in self.reassignment_actions:
            recovery.entry_block.add_instr(OpInstr(
                op_type, None, [role], params=description,
                expected_time=self.spec.action_time(op_type, description)))
        lowering = TargetLowering(self.f_t).lower(recovery)
        return self._execute_recovery_actions(recovery.entry_block.instructions, lowering, t,
                                              station_busy_until, station_busy_total, agv_free)

    def _execute_recovery_actions(self, instructions, lowering, t,
                                  station_busy_until, station_busy_total, agv_free):
        charged = 0.0
        ready = t
        for instr in instructions:
            lowered = lowering.operations[id(instr)]
            duration = float(instr.expected_time or 0.0)
            if duration <= 0.0:
                continue
            if lowered.is_logistics:
                logistics = self.f_t.logistics_resource()
                available = self.f_t.state.unavailable_until.get((AVAIL_LOGISTICS, logistics.palu_id), 0.0)
                index = min(range(len(agv_free)), key=lambda i: agv_free[i])
                ready = max(ready, agv_free[index], available) + duration
                agv_free[index] = ready
            else:
                start, ws_id, index = min(
                    (max(ready, free, self.f_t.state.unavailable_until.get((AVAIL_WORKSTATION, ws), 0.0)), ws, i)
                    for ws in lowered.candidate_workstations
                    for i, free in enumerate(station_busy_until[ws]))
                ready = start + duration
                station_busy_until[ws_id][index] = ready
                station_busy_total[ws_id] += duration
            charged += duration
        return charged, ready

    def _apply_production_block(self, event: UncertaintyEvent,
                                station_busy_until: Dict[int, List[float]], t: float) -> None:
        """资源态事件：把设备/库位/物流资源标记为不可用。"""
        if not event.type.invalidates_resource or event.resolves_at is None:
            return
        if event.target_kind == TARGET_WORKSTATION:
            self.f_t.mark_unavailable(AVAIL_WORKSTATION, event.target, event.resolves_at)
        elif event.target_kind == TARGET_HOLDING:
            from compiler.factory_target_machine import AVAIL_HOLDING
            self.f_t.mark_unavailable(AVAIL_HOLDING, event.target, event.resolves_at)
        elif event.target_kind == TARGET_LOGISTICS:
            from compiler.factory_target_machine import AVAIL_LOGISTICS
            self.f_t.mark_unavailable(AVAIL_LOGISTICS, event.target, event.resolves_at)

    def _transport_time(self, source_site: Optional[int], station: int) -> float:
        """一次搬运的耗时：取放料 + 每格行走时间 × 两工位间的曼哈顿距离。"""
        if source_site is None:
            return 0.0
        src = self.f_t.holding_site(source_site)
        if src is None or src.workstation_id == station:
            return 0.0
        a = self.ws_coords.get(src.workstation_id)
        b = self.ws_coords.get(station)
        if a is None or b is None:
            return 0.0
        steps = abs(a[0] - b[0]) + abs(a[1] - b[1])
        return self.config.transport_factor * (
            self.config.transport_setup + self.config.transport_per_step * steps)


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------
def _ordered_ops(compiled: CompiledOrderProgram) -> Tuple[List[OpInstr], List[int]]:
    """按工序顺序列出该订单的实体动作指令，并给出每条指令所属的逻辑工序号。

    catch / compensate 分支里的指令属于异常路径，只在处理器被激活时才执行，
    不能混进正常派工序列。
    """
    by_id = {}
    for block in compiled.cfg.blocks:
        if _is_handler_block(block):
            continue
        for instr in block.instructions:
            if isinstance(instr, OpInstr):
                by_id[id(instr)] = instr
    ops: List[OpInstr] = []
    seqs: List[int] = []
    for op in compiled.program.operations:
        for instr_id in compiled.op_instr_ids.get(op.key, []):
            instr = by_id.get(instr_id)
            if instr is None:
                continue
            # `Move` 不进派工序列：着色器插入的跨工位搬运由仿真**单独建模**
            # （`transport_until` + AGV 占用）。把它也当成可派工工序，既重复计算搬运，
            # 又会因为着色器插入的搬运指令没有工位绑定而永远派不出去、把订单卡死。
            if instr.op_type in ("Process", "Inspect", "Load", "Store", "Adjust"):
                ops.append(instr)
                seqs.append(op.seq)
    return ops, seqs


def _lowering_of(item: OrderRuntime):
    return item.lowering


def _op_station(item: OrderRuntime, seq: int) -> Optional[int]:
    if seq < 0 or seq >= len(item.ops):
        return None
    station = getattr(item.ops[seq], "assigned_workstation_id", None)
    return station


# 不处置就会在制品上留下后果的异常：不纠正就一路流到检验环节显形并返工。
#  · 工装偏位 / 质量偏差 —— 已经做出来的东西有缺陷；
#  · 机器故障 —— 工序被中途打断，制品停在不确定状态，必须重做或复验。
# 缺料不在此列：那是"料没到"，直接卡住生产，不会做出带缺陷的制品。
LATENT_DEFECT_TYPES = ("ToolingDeviation", "QualityFault")
# 料没到：直接卡住生产，不会做出带缺陷的制品
BLOCKING_SHORTAGE_TYPES = ("MaterialMissing",)


def _is_blocking_shortage(event: UncertaintyEvent) -> bool:
    return event.type.exception_type in BLOCKING_SHORTAGE_TYPES


def _is_latent_defect(event: UncertaintyEvent) -> bool:
    """该异常被跳过时会不会留下带缺陷的制品（区别于"料没到"这类阻塞型异常）。"""
    return (event.type.exception_type in LATENT_DEFECT_TYPES or
            event.type.exception_type == "MachineFailure" and event.detail.get("interrupted", False))


def handler_reachable_blocks(block, name_prefix: str):
    """
    catch 块本体，以及被截断拆出去的续块（`Catch_X_Fallthrough` 之类）。

    截断可能连续发生多次，续块是**链式**的，必须沿后继一路取完——
    只看直接后继会漏掉第二段之后的处置工序。
    """
    blocks = [block]
    seen = {id(block)}
    queue = list(getattr(block, "successors", ()) or ())
    while queue:
        candidate = queue.pop(0)
        if id(candidate) in seen:
            continue
        if not (getattr(candidate, "name", "") or "").startswith(name_prefix):
            continue
        seen.add(id(candidate))
        blocks.append(candidate)
        queue.extend(getattr(candidate, "successors", ()) or ())
    return blocks


def _owns_any(item: OrderRuntime, vregs) -> bool:
    """这些物料版本里有没有属于该订单的。

    物料版本形如 `raw_Order_0000_12`，去掉尾部版本号得到物料身份
    `raw_Order_0000`；用**后缀**匹配订单号，避免 `Order_0001` 命中 `Order_00001`。
    """
    if not vregs:
        return False
    order_id = item.compiled.program.order_id
    for vreg in vregs:
        if isinstance(vreg, str) and _material_family(vreg).endswith(order_id):
            return True
    return False


def _any_held_site(held: Mapping[str, int]) -> Optional[int]:
    for site in held.values():
        if isinstance(site, int):
            return site
    return None


def _op_defs(op: OpInstr) -> List[str]:
    """一条指令定义（产出）的物料版本。"""
    defs = [v for v in (op.implicit_targets or []) if isinstance(v, str)]
    if isinstance(op.target, str) and op.target not in defs:
        defs.append(op.target)
    return defs


def _output_site(op: OpInstr, allocation: Mapping[str, int]) -> Optional[int]:
    """该指令产出物料本轮被落到的库位。"""
    for vreg in _op_defs(op):
        site = allocation.get(vreg)
        if isinstance(site, int):
            return site
    return None


def _material_family(vreg: str) -> str:
    """物料版本号去掉后的物料身份：`raw_Order_0000_12` -> `raw_Order_0000`。"""
    head, _, tail = vreg.rpartition('_')
    return head if head and tail.isdigit() else vreg


def _held_after(item: OrderRuntime, op: OpInstr, site: Optional[int]) -> Dict[str, int]:
    """一道工序做完之后，该订单实际占用的库位。

    同一件物料的新版本取代旧版本：旧版本被消耗掉（位置空出来），
    新版本占住这道工序的落位。
    """
    defs = _op_defs(op)
    families = {_material_family(v) for v in defs}
    held = {v: s for v, s in item.held.items() if _material_family(v) not in families}
    for vreg in defs:
        if site is not None:
            held[vreg] = site
    return held


def _next_output_versions(item: OrderRuntime) -> List[str]:
    """该订单下一道待加工工序将要产出的物料版本。"""
    index = item.completed_seq + 1
    if index >= len(item.ops):
        return []
    return _op_defs(item.ops[index])


def _logical_seq(item: OrderRuntime, index: int) -> int:
    """已完成到的最后一条**逻辑**工序号。

    一条逻辑工序可能对应多条实体指令。Workset Composer 按 `seq <= completed_seq`
    冻结工序，因此只有该工序的全部指令都做完，它才能算完成；否则下一轮着色会把
    尚未执行的那条指令的产出物料一起冻掉，绑定随之丢失。
    """
    if index < 0 or not item.op_seqs:
        return -1
    index = min(index, len(item.op_seqs) - 1)
    seq = item.op_seqs[index]
    if index + 1 < len(item.op_seqs) and item.op_seqs[index + 1] == seq:
        return seq - 1
    return seq


def _op_start(item: OrderRuntime) -> float:
    return float(getattr(item, "op_start", item.executing_until))


def _op_duration(op: OpInstr, config: Study2Config, seed: int, order_id: int, seq: int) -> float:
    """处理时间扰动：确定性，且不依赖内置 hash（跨进程必须可复现）。"""
    base = float(op.expected_time or 1.0)
    jitter = 1.0 + config.processing_cv * (2.0 * _rng(seed, order_id * 31 + seq, 11) - 1.0)
    return max(0.1, base * jitter)


def _rng(seed: int, a: int, b: int) -> float:
    """确定性的 [0, 1) 伪随机数，用于到达间隔与恢复时长。"""
    value = (seed * 6364136223846793005 + a * 1442695040888963407 + b * 2654435761) & 0xFFFFFFFF
    return (value % 1000003) / 1000003.0


def _logistics_count(target_model) -> int:
    ics = target_model.data.get("instruction_capability_set") or {}
    for palu in ics.get("palu_definitions", []):
        if "AGV" in str(palu.get("device_type", "")).upper():
            return int(palu.get("quantity", 1) or 1)
    return 1


def _bind_target(event: UncertaintyEvent, target) -> UncertaintyEvent:
    """把事件挂到具体的资源/订单上，并带上它自己的资源种类。"""
    order_id = event.order_id
    if event.type.target_kind == "material" and isinstance(target, str):
        order_id = target
    return replace(event, order_id=order_id, target_kind=event.type.target_kind, target=target)


def _with_arrival(order: DomainOrder, arrival: float, due: float) -> DomainOrder:
    return DomainOrder(
        domain=order.domain, order_id=order.order_id, arrival_time=arrival,
        due_time=due, quality_grade=order.quality_grade, options=order.options,
    )


def _workset_signature(admitted: Sequence[OrderRuntime]) -> Tuple:
    return tuple(sorted((item.compiled.program.order_id, item.completed_seq,
                         item.executing, item.in_transit, item.material_location, item.dispatch_site)
                        for item in admitted))


def _availability_signature(f_t, t: float) -> Tuple:
    """
    当前资源可用性快照。

    资源**恢复**时不会产生任何事件，只会让 `is_available` 从假变真。若不把它
    纳入重着色触发条件，停机期间被判定为 WAIT 的物料版本就再也不会被重新考虑，
    订单会永久卡住（区域重编译也一样：没有新的落位决策可用）。
    """
    return tuple(sorted(
        (str(key), f_t.is_available(key[0], key[1], t))
        for key in f_t.state.unavailable_until
    ))


def item_is_open(admitted: Sequence[OrderRuntime]) -> bool:
    return any(item.finish_time is None for item in admitted)
