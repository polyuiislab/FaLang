# F_IDE_Project/case_study/domains/orders.py
"""
跨 domain 的定制订单生成。

制造语境：无论哪个门类，"定制"都表现为订单在某几个可配置族上各挑一个取值，
再叠加一个质量等级。订单到达是随机的，交期由订单自身的工作量与车间产能共同决定。
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from case_study.domains.spec import DomainSpec


# 各质量等级的抽样权重与相对检测强度
GRADE_WEIGHTS: Mapping[str, float] = {"basic": 5.0, "standard": 2.0}
GRADE_LOAD_FACTOR: Mapping[str, float] = {"low": 1.0, "medium": 1.2, "high": 1.35}


@dataclass(frozen=True)
class DomainOrder:
    """一张定制订单。与具体制造门类无关。"""

    domain: str
    order_id: int
    arrival_time: float
    due_time: float
    quality_grade: str
    options: Mapping[str, str]

    @property
    def task_name(self) -> str:
        return f"Order_{self.order_id:04d}"

    @property
    def components(self) -> Mapping[str, str]:
        """与既有工艺规则接口保持一致。"""
        return self.options

    def option_signature(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(self.options.items()))


class DomainOrderGenerator:
    """
    按 domain 的可配置族生成订单。

    到达间隔服从指数分布（泊松到达）；交期按名义提前期的一个对称区间给出，
    这一点与制造文献的常规做法一致，避免把交期设成产能的硬约束。
    """

    def __init__(self, spec: DomainSpec, seed: int, load: str = "medium"):
        if load not in spec.arrival_mtba:
            raise KeyError(f"load {load!r} not configured for domain {spec.key!r}")
        self.spec = spec
        self.seed = seed
        self.load = load
        self.mtba = float(spec.arrival_mtba[load])

    def generate(self, count: int, program_generator=None) -> List[DomainOrder]:
        rng = random.Random(self.seed)
        grades = list(self.spec.quality_grades)
        weights = [GRADE_WEIGHTS.get(grade, 1.0) for grade in grades]
        load_factor = GRADE_LOAD_FACTOR.get(self.load, 1.2)

        orders: List[DomainOrder] = []
        arrival = 0.0
        for order_id in range(count):
            arrival += rng.expovariate(1.0 / self.mtba)
            options = {
                family.family: rng.choice(list(family.models))
                for family in self.spec.option_families
            }
            grade = rng.choices(grades, weights=weights, k=1)[0]
            order = DomainOrder(
                domain=self.spec.key,
                order_id=order_id,
                arrival_time=round(arrival, 3),
                due_time=0.0,
                quality_grade=grade,
                options=options,
            )
            lead = self._nominal_lead_time(order, program_generator) * load_factor
            due = arrival + rng.uniform(0.8, 1.6) * lead
            orders.append(DomainOrder(
                domain=self.spec.key,
                order_id=order_id,
                arrival_time=order.arrival_time,
                due_time=round(due, 3),
                quality_grade=grade,
                options=options,
            ))
        return orders

    def _nominal_lead_time(self, order: DomainOrder, program_generator) -> float:
        if program_generator is not None:
            return program_generator.nominal_lead_time(order)
        # 没有生成器时退回按动作时间表估算
        total = sum(action.processing_time for action in self.spec.actions)
        return max(1.0, total / max(1, len(self.spec.workstations)))


def paired_domain_order_sets(
    spec: DomainSpec,
    seeds: Sequence[int],
    count: int,
    load: str,
    program_generator=None,
) -> Dict[int, List[DomainOrder]]:
    """同一 seed 下所有方法看到完全相同的订单集合。"""
    return {
        seed: DomainOrderGenerator(spec, seed=seed, load=load).generate(count, program_generator)
        for seed in seeds
    }
