"""Domain specifications, order generators, and target-model serialization."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, List, Tuple

from case_study.domains import (
    cnc_machining, garment, garment_compact, garment_continuation, garment_overlap, pc_assembly,
)
from case_study.domains.generator import DomainProgramGenerator, rule_library_from_spec
from case_study.domains.orders import DomainOrder, DomainOrderGenerator
from case_study.domains.spec import DomainSpec, build_target_model

DOMAIN_MODULES = (pc_assembly, garment, cnc_machining)
DOMAIN_SPECS: Dict[str, DomainSpec] = {module.SPEC.key: module.SPEC for module in DOMAIN_MODULES}
DOMAIN_KEYS: Tuple[str, ...] = tuple(DOMAIN_SPECS)
DOMAIN_SPECS[garment_compact.SPEC.key] = garment_compact.SPEC
DOMAIN_SPECS[garment_overlap.SPEC.key] = garment_overlap.SPEC
DOMAIN_SPECS[garment_continuation.SPEC.key] = garment_continuation.SPEC
DOMAIN_SPECS[garment_continuation.COMPACT_SPEC.key] = garment_continuation.COMPACT_SPEC
DOMAIN_SPECS[garment_continuation.RETOOLING_SPEC.key] = garment_continuation.RETOOLING_SPEC

MODEL_DIR = Path(__file__).resolve().parents[2] / "domain_models"


def spec_of(key: str) -> DomainSpec:
    if key not in DOMAIN_SPECS:
        raise KeyError(f"unknown domain {key!r}; known: {DOMAIN_KEYS}")
    return DOMAIN_SPECS[key]


def program_generator(key: str) -> DomainProgramGenerator:
    return DomainProgramGenerator(spec_of(key))


def target_model_path(key: str, materialize: bool = True) -> Path:
    """Return the model path, optionally updating it from the domain specification."""
    path = MODEL_DIR / f"{key}.json"
    if materialize:
        write_target_model(key)
    return path


def write_target_model(key: str) -> Path:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    path = MODEL_DIR / f"{key}.json"
    payload = json.dumps(_model_of(key), indent=2, ensure_ascii=False) + chr(10)
    if path.exists() and path.read_text(encoding="utf-8") == payload:
        return path
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)
    return path


def _model_of(key: str) -> Dict[str, object]:
    return build_target_model(spec_of(key))


def materialize_all(force: bool = True) -> List[Path]:
    return [write_target_model(key) for key in DOMAIN_KEYS]
