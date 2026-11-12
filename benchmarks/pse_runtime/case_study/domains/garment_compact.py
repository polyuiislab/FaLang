"""Study 3 试验配置：每工位一个线边库位，每库位容量仍为 2。"""
from dataclasses import replace

from case_study.domains.garment import SPEC as GARMENT_SPEC

SPEC = replace(
    GARMENT_SPEC,
    key="garment_compact",
    holding_slots_per_workstation=1,
    workstations=tuple(replace(ws, registers=ws.registers[:1]) for ws in GARMENT_SPEC.workstations),
)
