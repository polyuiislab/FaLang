# F_IDE_Project/compiler/uncertainty.py
"""
Paper 1 —— 不确定性事件与响应策略。

制造语境：车间里的意外种类很多，但**种类本身并不决定该怎么应对**。

- 设备故障可以被事先安排的处置转移掉——订单程序里把这道工序写进 try，
  catch 里在同类工位上重做一遍即可；
- 物料短缺也可能超出事先安排的范围——补料动作本身要占用物流与库位，
  一个工序的缺料可能让整段工位一起饿料。

因此本模块**不按事件种类给机制分工**。决定分工的是事件在上下文中的三个属性：

1. **anticipation**（设计时判定）：该程序点上是否已为该不确定性预定义了处理器
   —— 也就是生成订单程序时有没有为这道工序写下 `catch`。
2. **executability at trigger**（触发时判定）：处理器的动作集此刻是否拿得到物理资源。
   注意这不是一道闸门：处理器的物料与其它残余物料一样进入残余工作集，
   由同一个图着色落位，拿不到资源就 WAIT，而不是让预定义反应失效。
3. **containment**（触发后判定）：处理器处置完毕，其链条之外的物料版本的可行位置集合
   是否仍然有效。这一条把"区域边界"从一个启发式选择变成可计算判据——
   区域由**候选集被实际作废的版本**定义。

事件种类只用于描述与生成。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple


# ----------------------------------------------------------------------
# 事件种类（描述性）
# ----------------------------------------------------------------------
class UncertaintyKind(str, Enum):
    MATERIAL_SHORTAGE = "material_shortage"
    QUALITY_FAULT = "quality_fault"
    TOOLING_DEVIATION = "tooling_deviation"
    MACHINE_FAILURE = "machine_failure"
    BUFFER_UNAVAILABLE = "buffer_unavailable"
    LOGISTICS_DEPLETION = "logistics_depletion"


# 事件的资源作用对象
TARGET_WORKSTATION = "workstation"
TARGET_HOLDING = "holding"
TARGET_LOGISTICS = "logistics"
TARGET_MATERIAL = "material"


@dataclass(frozen=True)
class UncertaintyType:
    """一类不确定性。"""

    key: str
    kind: UncertaintyKind
    label: str
    # 订单程序里对应的 FaLang 异常类型；None 表示这类事件不由 catch 承载
    exception_type: Optional[str] = None
    # 事件本身是否直接作废某个资源（设备/库位/物流资源）
    invalidates_resource: bool = False
    target_kind: Optional[str] = None
    # 是否可作为"备用资源可用时转移处置"的对象
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

# 两种事件组合：Scenario A 以 anticipated 事件为主，Scenario B 以资源态事件为主。
# 组合只影响**事件的种类构成**，不决定机制分工。
GROUP_A: Tuple[UncertaintyType, ...] = (U_MATERIAL_SHORTAGE, U_QUALITY_FAULT, U_TOOLING_DEVIATION)
GROUP_B: Tuple[UncertaintyType, ...] = (U_MACHINE_FAILURE, U_BUFFER_UNAVAILABLE,
                                        U_LOGISTICS_DEPLETION)
ALL_TYPES: Tuple[UncertaintyType, ...] = GROUP_A + GROUP_B
_TYPES_BY_KEY: Dict[str, UncertaintyType] = {t.key: t for t in ALL_TYPES}


def type_by_key(key: str) -> UncertaintyType:
    if key not in _TYPES_BY_KEY:
        raise KeyError(f"Unknown uncertainty type: {key}")
    return _TYPES_BY_KEY[key]


# ----------------------------------------------------------------------
# 事件
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class UncertaintyEvent:
    """一次不确定性事件。"""

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


# ----------------------------------------------------------------------
# 事件在上下文中的属性 —— 机制分工的真正依据
# ----------------------------------------------------------------------
@dataclass
class EventAttributes:
    """事件在上下文中的属性。"""

    # 设计时：该程序点上是否预定义了处理器
    anticipated: bool = False
    handler_region: Optional[str] = None
    handler_exception_type: Optional[str] = None
    # 触发时：处理器的动作集此刻是否拿得到物理资源
    executable_at_trigger: bool = False
    # 触发后：处理器链条之外的物料版本，其可行位置集合是否仍然有效
    contained: bool = False
    # 供解释用的计数
    uncovered_versions: int = 0

    def snapshot(self) -> Dict[str, object]:
        return {
            "anticipated": self.anticipated,
            "region": self.handler_region,
            "executable_at_trigger": self.executable_at_trigger,
            "contained": self.contained,
            "uncovered_versions": self.uncovered_versions,
        }


# ----------------------------------------------------------------------
# 策略
# ----------------------------------------------------------------------
@dataclass(frozen=True)
class HandlingPlan:
    """某策略在该属性下准备怎么处置。"""

    use_handler: bool
    recompile: bool
    recompile_scope: str = ""     # "" / "whole_region" / "uncovered_part"

    def snapshot(self) -> Dict[str, object]:
        return {
            "use_handler": self.use_handler,
            "recompile": self.recompile,
            "scope": self.recompile_scope,
        }


class ResponsePolicy(str, Enum):
    """三类响应策略。三者都在同一套预编译处理器与同一套区域重编译之上。"""

    # 不启用预编译处理器；任何事件都直接重编译受影响区域
    REACTIVE = "reactive"
    # 只要存在处理器就启用，不做任何重编译；兜不住的部分只能等待
    PREDEFINITION = "predefinition"
    # 启用处理器；只对未被兜住的残余部分重编译
    HYBRID = "hybrid"

    def plan(self, attributes: EventAttributes) -> HandlingPlan:
        if self is ResponsePolicy.REACTIVE:
            return HandlingPlan(use_handler=False, recompile=True, recompile_scope="whole_region")
        if self is ResponsePolicy.PREDEFINITION:
            return HandlingPlan(use_handler=attributes.anticipated, recompile=False)
        # HYBRID
        if not attributes.anticipated:
            return HandlingPlan(use_handler=False, recompile=True, recompile_scope="whole_region")
        if not attributes.executable_at_trigger:
            # 处理器写在程序里，但它的动作集此刻拿不到物理资源——它根本跑不起来。
            # 此时"启用处理器"是空的，handler 承载的处置意图必须靠重编译重新取得资源。
            # 这条与 anticipation、containment 并列，是 RQ2 三条判据里的第二条。
            return HandlingPlan(use_handler=False, recompile=True, recompile_scope="whole_region")
        return HandlingPlan(
            use_handler=True,
            recompile=not attributes.contained,
            recompile_scope="" if attributes.contained else "uncovered_part",
        )


# ----------------------------------------------------------------------
# 事件流
# ----------------------------------------------------------------------
@dataclass
class UncertaintyStream:
    """
    确定性的不确定性事件流。

    Mixed 场景按**事件种类的组成比例**抽取：先按比例决定这次来自 A 组合还是 B 组合，
    再从该组合里选具体的一种。比例之和为 1，不是两个独立的发生概率。
    """

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
