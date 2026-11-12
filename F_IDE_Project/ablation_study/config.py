from __future__ import annotations

from copy import deepcopy
from typing import Dict, Tuple


ABLATION_METHODS: Tuple[str, ...] = (
    "falang",
    "no_pre_ra",
    "no_pbqp",
)


ABLATION_METHOD_CONFIGS: Dict[str, Dict[str, object]] = {
    "falang": {},
    "no_pre_ra": {
        "base_method": "falang",
        "method_seed_index": 1,
        "defer_initial_compile": True,
        "initial_compile_options": {
            "skip_pre_ra": True,
            "allocation_mode": "pbqp",
        },
        "recompile_options": {
            "skip_pre_ra": True,
            "allocation_mode": "pbqp",
        },
        "use_catch_plans": False,
        "runtime_penalties": {
            "logistics_factor": 0.10,
            "waiting_factor": 0.05,
        },
    },
    "no_pbqp": {
        "base_method": "falang",
        "method_seed_index": 2,
        "defer_initial_compile": True,
        "initial_compile_options": {
            "allocation_mode": "graph_coloring",
            "enable_try_catch": True,
        },
        "recompile_options": {
            "allocation_mode": "graph_coloring",
            "enable_try_catch": True,
        },
        "use_catch_plans": True,
        "runtime_penalties": {
            "logistics_factor": 0.16,
            "waiting_factor": 0.08,
        },
    },
}


def method_config(method: str) -> Dict[str, object]:
    return deepcopy(ABLATION_METHOD_CONFIGS[method])
