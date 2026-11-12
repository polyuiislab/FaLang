from __future__ import annotations

from dataclasses import dataclass
import random
from typing import Dict, Iterable, List


LOAD_MTBA = {
    "low": 18.0,
    "medium": 13.0,
    "high": 1.4,
}

QUALITY_GRADES = {
    "basic": {"tests": ["Power_On_Test"], "quality_fault_probability": 0.03},
    "standard": {"tests": ["Power_On_Test", "Benchmark_Test"], "quality_fault_probability": 0.05},
    "premium": {"tests": ["Power_On_Test", "Benchmark_Test", "Thermal_Test"], "quality_fault_probability": 0.07},
}

QUALITY_GRADE_WEIGHTS = {
    "basic": 5.51,
    "standard": 1.80,
    "premium": 2.22,
}


@dataclass(frozen=True)
class PCOrder:
    order_id: int
    arrival_time: float
    due_time: float
    quality_grade: str
    components: Dict[str, str]
    nominal_lead_time: float

    @property
    def task_name(self) -> str:
        return f"Order_{self.order_id:04d}"


class PCOrderGenerator:
    """Reproducible customized-order generator for the PC-MMS case study."""

    cpu_models = [f"CPU_{i}" for i in range(1, 6)]
    motherboard_models = [f"MB_{i}" for i in range(1, 5)]
    ram_models = [f"RAM_{i}" for i in range(1, 5)]
    gpu_models = ["GPU_NONE"] + [f"GPU_{i}" for i in range(1, 5)]
    storage_models = [f"SSD_{i}" for i in range(1, 5)]
    cooling_models = ["COOLING_NONE", "COOLING_1", "COOLING_2"]

    def __init__(self, seed: int, load: str = "medium"):
        if load not in LOAD_MTBA:
            raise ValueError(f"Unknown load scenario: {load}")
        self.random = random.Random(seed)
        self.load = load
        self.mtba = LOAD_MTBA[load]

    def generate(self, count: int = 200) -> List[PCOrder]:
        arrival = 0.0
        orders: List[PCOrder] = []
        for order_id in range(count):
            arrival += self.random.expovariate(1.0 / self.mtba)
            components = self._sample_components()
            grade_names = list(QUALITY_GRADES)
            grade = self.random.choices(
                grade_names,
                weights=[QUALITY_GRADE_WEIGHTS[name] for name in grade_names],
                k=1,
            )[0]
            nominal = self._nominal_lead_time(components, grade)
            due_time = arrival + self.random.uniform(0.4, 1.0) * nominal
            orders.append(
                PCOrder(
                    order_id=order_id,
                    arrival_time=arrival,
                    due_time=due_time,
                    quality_grade=grade,
                    components=components,
                    nominal_lead_time=nominal,
                )
            )
        return orders

    def _sample_components(self) -> Dict[str, str]:
        return {
            "Motherboard": self.random.choice(self.motherboard_models),
            "CPU": self.random.choice(self.cpu_models),
            "RAM": self.random.choice(self.ram_models),
            "Storage": self.random.choice(self.storage_models),
            "GPU": self.random.choice(self.gpu_models),
            "Cooling": self.random.choice(self.cooling_models),
            "PSU": "PSU_1",
            "Case": "CASE_1",
        }

    def _nominal_lead_time(self, components: Dict[str, str], quality_grade: str) -> float:
        assembly_time = 12 + 8 + 6 + 5 + 5 + 6
        if components["GPU"] != "GPU_NONE":
            assembly_time += 10
        if components["Cooling"] != "COOLING_NONE":
            assembly_time += 7

        test_time = 0
        for test in QUALITY_GRADES[quality_grade]["tests"]:
            test_time += {"Power_On_Test": 6, "Benchmark_Test": 30, "Thermal_Test": 60}[test]

        packaging_time = 12
        logistics_allowance = 8 * (7 + len(QUALITY_GRADES[quality_grade]["tests"]))
        average_setup_allowance = 18
        load_factor = {"low": 1.0, "medium": 1.2, "high": 1.4}[self.load]
        return (assembly_time + test_time + packaging_time + logistics_allowance + average_setup_allowance) * load_factor


def paired_order_sets(seeds: Iterable[int], load: str, count: int = 200) -> Dict[int, List[PCOrder]]:
    return {seed: PCOrderGenerator(seed, load).generate(count) for seed in seeds}
