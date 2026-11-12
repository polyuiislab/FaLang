"""Seeded order generation across manufacturing domains."""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from case_study.domains.spec import DomainSpec


# Sampling weights and inspection intensity for each quality grade.
GRADE_WEIGHTS: Mapping[str, float] = {"basic": 5.0, "standard": 2.0}
GRADE_LOAD_FACTOR: Mapping[str, float] = {"low": 1.0, "medium": 1.2, "high": 1.35}


@dataclass(frozen=True)
class DomainOrder:

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
        return self.options

    def option_signature(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(self.options.items()))


class DomainOrderGenerator:
    """Sample configuration options, Poisson arrivals, and due-date allowances."""

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
        total = sum(action.processing_time for action in self.spec.actions)
        return max(1.0, total / max(1, len(self.spec.workstations)))


def paired_domain_order_sets(
    spec: DomainSpec,
    seeds: Sequence[int],
    count: int,
    load: str,
    program_generator=None,
) -> Dict[int, List[DomainOrder]]:
    """Generate the same order set for every method at a given seed."""
    return {
        seed: DomainOrderGenerator(spec, seed=seed, load=load).generate(count, program_generator)
        for seed in seeds
    }
