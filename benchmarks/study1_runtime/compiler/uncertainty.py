"""Represent uncertainty events and select handling from anticipation, executability, and coverage."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple


class UncertaintyKind(str, Enum):
    MATERIAL_SHORTAGE = "material_shortage"
    QUALITY_FAULT = "quality_fault"
    TOOLING_DEVIATION = "tooling_deviation"
    MACHINE_FAILURE = "machine_failure"
    BUFFER_UNAVAILABLE = "buffer_unavailable"
    LOGISTICS_DEPLETION = "logistics_depletion"


TARGET_WORKSTATION = "workstation"
TARGET_HOLDING = "holding"
TARGET_LOGISTICS = "logistics"
TARGET_MATERIAL = "material"


@dataclass(frozen=True)
class UncertaintyType:


    key: str
    kind: UncertaintyKind
    label: str
    # FaLang exception type, or None for an event outside catch handling.
    exception_type: Optional[str] = None

    invalidates_resource: bool = False
    target_kind: Optional[str] = None

    divertible: bool = False


U_MATERIAL_SHORTAGE = UncertaintyType(
    key=UncertaintyKind.MATERIAL_SHORTAGE.value,
    kind=UncertaintyKind.MATERIAL_SHORTAGE,
    label="物料短缺，随后补料",
    exception_type="MaterialMissing",
    invalidates_resource=False,
    target_kind=TARGET_MATERIAL,
    divertible=False,
)
U_QUALITY_FAULT = UncertaintyType(
    key=UncertaintyKind.QUALITY_FAULT.value,
    kind=UncertaintyKind.QUALITY_FAULT,
    label="局部质量不合格，按预定义返工",
    exception_type="QualityFault",
    invalidates_resource=False,
    target_kind=TARGET_MATERIAL,
    divertible=False,
)
U_TOOLING_DEVIATION = UncertaintyType(
    key=UncertaintyKind.TOOLING_DEVIATION.value,
    kind=UncertaintyKind.TOOLING_DEVIATION,
    label="工装/夹具偏位，需要调整",
    exception_type="ToolingDeviation",
    invalidates_resource=False,
    target_kind=TARGET_WORKSTATION,
    divertible=False,
)
U_MACHINE_FAILURE = UncertaintyType(
    key=UncertaintyKind.MACHINE_FAILURE.value,
    kind=UncertaintyKind.MACHINE_FAILURE,
    label="设备故障",
    exception_type="MachineFailure",
    invalidates_resource=True,
    target_kind=TARGET_WORKSTATION,
    divertible=True,
)
U_BUFFER_UNAVAILABLE = UncertaintyType(
    key=UncertaintyKind.BUFFER_UNAVAILABLE.value,
    kind=UncertaintyKind.BUFFER_UNAVAILABLE,
    label="共享缓存不可用",
    exception_type="BufferUnavailable",
    invalidates_resource=True,
    target_kind=TARGET_HOLDING,
    divertible=True,
)
U_LOGISTICS_DEPLETION = UncertaintyType(
    key=UncertaintyKind.LOGISTICS_DEPLETION.value,
    kind=UncertaintyKind.LOGISTICS_DEPLETION,
    label="物流资源电力耗尽（例如 AGV 电池耗尽）",
    exception_type="LogisticsDepleted",
    invalidates_resource=True,
    target_kind=TARGET_LOGISTICS,
    divertible=False,
)

# Scenario mixtures control event composition; contextual attributes select the handling policy.

GROUP_A: Tuple[UncertaintyType, ...] = (U_MATERIAL_SHORTAGE, U_QUALITY_FAULT, U_TOOLING_DEVIATION)
GROUP_B: Tuple[UncertaintyType, ...] = (U_MACHINE_FAILURE, U_BUFFER_UNAVAILABLE,
                                        U_LOGISTICS_DEPLETION)
ALL_TYPES: Tuple[UncertaintyType, ...] = GROUP_A + GROUP_B
_TYPES_BY_KEY: Dict[str, UncertaintyType] = {t.key: t for t in ALL_TYPES}


def type_by_key(key: str) -> UncertaintyType:
    if key not in _TYPES_BY_KEY:
        raise KeyError(f"Unknown uncertainty type: {key}")
    return _TYPES_BY_KEY[key]


@dataclass(frozen=True)
class UncertaintyEvent:


    event_id: str
    type: UncertaintyType
    time: float
    order_id: Optional[str] = None
    op_key: Optional[str] = None
    target_kind: Optional[str] = None
    target: Optional[object] = None
    resolves_at: Optional[float] = None
    detail: Dict[str, object] = field(default_factory=dict)

    def snapshot(self) -> Dict[str, object]:
        return {
            "event_id": self.event_id,
            "kind": self.type.kind.value,
            "time": self.time,
            "order_id": self.order_id,
            "op_key": self.op_key,
            "target": f"{self.target_kind}:{self.target}" if self.target_kind else None,
            "resolves_at": self.resolves_at,
        }


@dataclass
class EventAttributes:
    """Handler availability and coverage evaluated in the current execution context."""

    # A handler is predefined at the triggering program point.
    anticipated: bool = False
    handler_region: Optional[str] = None
    handler_exception_type: Optional[str] = None
    # The fixed handler resource plan is executable at the trigger time.
    executable_at_trigger: bool = False
    # Allocations outside handler coverage remain feasible.
    contained: bool = False

    uncovered_versions: int = 0

    def snapshot(self) -> Dict[str, object]:
        return {
            "anticipated": self.anticipated,
            "region": self.handler_region,
            "executable_at_trigger": self.executable_at_trigger,
            "contained": self.contained,
            "uncovered_versions": self.uncovered_versions,
        }


@dataclass(frozen=True)
class HandlingPlan:


    use_handler: bool
    recompile: bool
    recompile_scope: str = ""     

    def snapshot(self) -> Dict[str, object]:
        return {
            "use_handler": self.use_handler,
            "recompile": self.recompile,
            "scope": self.recompile_scope,
        }


class ResponsePolicy(str, Enum):
    """Select predefined handling, regional recompilation, or their combination."""


    REACTIVE = "reactive"

    PREDEFINITION = "predefinition"

    HYBRID = "hybrid"

    def plan(self, attributes: EventAttributes) -> HandlingPlan:
        if self is ResponsePolicy.REACTIVE:
            return HandlingPlan(use_handler=False, recompile=True, recompile_scope="whole_region")
        if self is ResponsePolicy.PREDEFINITION:
            return HandlingPlan(use_handler=attributes.anticipated, recompile=False)

        if not attributes.anticipated:
            return HandlingPlan(use_handler=False, recompile=True, recompile_scope="whole_region")
        if not attributes.executable_at_trigger:


            return HandlingPlan(use_handler=False, recompile=True, recompile_scope="whole_region")
        return HandlingPlan(
            use_handler=True,
            recompile=not attributes.contained,
            recompile_scope="" if attributes.contained else "uncovered_part",
        )


@dataclass
class UncertaintyStream:
    """Generate seeded event streams by sampling event-group proportions, then an event within the group."""

    seed: int
    group_a_share: float = 1.0
    group_a: Sequence[UncertaintyType] = GROUP_A
    group_b: Sequence[UncertaintyType] = GROUP_B

    def __post_init__(self):
        if not 0.0 <= self.group_a_share <= 1.0:
            raise ValueError("group_a_share must be within [0, 1]")

    def generate(self, count: int, time_span: Tuple[float, float], target_picker=None
                 ) -> List[UncertaintyEvent]:
        import random

        if count <= 0:
            return []
        rng = random.Random(self.seed)
        start, end = time_span
        events: List[UncertaintyEvent] = []
        pool_a = list(self.group_a)
        pool_b = list(self.group_b)

        for index in range(count):
            pick = rng.random()
            if pool_a and (not pool_b or pick < self.group_a_share):
                chosen = rng.choice(pool_a)
            elif pool_b:
                chosen = rng.choice(pool_b)
            else:
                continue

            moment = start + (end - start) * (index + 1) / (count + 1)
            order_id = op_key = target_kind = target = None
            if target_picker is not None:
                picked = target_picker(index, chosen)
                if picked:
                    order_id, op_key, target_kind, target = (list(picked) + [None] * 4)[:4]
            events.append(UncertaintyEvent(
                event_id=f"{chosen.key}-{index:04d}",
                type=chosen,
                time=moment,
                order_id=order_id,
                op_key=op_key,
                target_kind=target_kind,
                target=target,
            ))
        return events

    def group_counts(self, count: int) -> Dict[str, int]:
        counts = {"A": 0, "B": 0}
        for event in self.generate(count, (0.0, 1.0)):
            counts["A" if event.type in GROUP_A else "B"] += 1
        return counts
