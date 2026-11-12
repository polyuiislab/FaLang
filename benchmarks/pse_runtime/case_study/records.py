"""Per-seed manufacturing metrics."""
from __future__ import annotations
from dataclasses import dataclass
from case_study.study2 import SimulationResult


@dataclass
class Study2Row:
    """一行结果：一个 (seed, policy) 单元。字段名对齐统计模块的取用方式。"""

    method: str
    # 区域边界规则：三种策略用 containment（本文方法），其余为文献基线
    region_rule: str
    seed: int
    domain: str
    orders: int
    load: str
    makespan: float
    mean_flow_time: float
    mean_tardiness: float
    on_time_delivery_rate: float
    resource_utilization: float
    feeding_window_utilization: float
    recovery_busy_time: float
    transport_time: float
    setup_time: float
    waiting_time: float
    uncertainty_events: int
    dispatch_repair_requests: int
    handler_activations: int
    recompilations: int
    rebound_versions: int
    recompilation_time_s: float
    # 车间量重规划代价：被改判落位、需重新下达指令并搬运的物料版本数；
    # 以及因此被打断在制品流转的工单数
    rehandled_versions: int
    disturbed_orders: int
    blocked_orders: int
    waiting_versions_peak: int
    colorings: int
    coloring_time_s: float
    compile_time_s: float
    wall_clock_s: float

    @classmethod
    def of(cls, result: SimulationResult, method: str | None = None,
           region_rule: str = "containment") -> "Study2Row":
        return cls(
            method=method or result.policy,
            region_rule=region_rule,
            seed=result.seed,
            domain=result.domain,
            orders=result.orders,
            load=result.load,
            makespan=result.makespan,
            mean_flow_time=result.mean_flow_time,
            mean_tardiness=result.mean_tardiness,
            on_time_delivery_rate=result.on_time_delivery_rate,
            resource_utilization=result.resource_utilization,
            feeding_window_utilization=result.feeding_window_utilization,
            recovery_busy_time=result.recovery_busy_time,
            transport_time=result.transport_time,
            setup_time=result.setup_time,
            waiting_time=result.waiting_time,
            uncertainty_events=result.uncertainty_events,
            dispatch_repair_requests=result.dispatch_repair_requests,
            handler_activations=result.handler_activations,
            recompilations=result.recompilations,
            rebound_versions=result.rebound_versions,
            recompilation_time_s=result.recompilation_time_s,
            rehandled_versions=result.rehandled_versions,
            disturbed_orders=result.disturbed_orders,
            blocked_orders=result.blocked_orders,
            waiting_versions_peak=result.waiting_versions_peak,
            colorings=result.colorings,
            coloring_time_s=result.coloring_time_s,
            compile_time_s=result.compile_time_s,
            wall_clock_s=result.wall_clock_s,
        )
