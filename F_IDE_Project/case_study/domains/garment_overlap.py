"""Line model with overlapping workstation capabilities and capacity two per holding site."""
from dataclasses import replace

from case_study.domains.garment_compact import SPEC as COMPACT_SPEC

PALUS = (
    (0, 3), (0, 1, 3), (0, 1, 3),
    (1, 2, 3), (1, 2, 4, 3), (1, 2, 4, 3),
    (4, 3, 5), (4, 6, 3, 5), (6, 3, 5),
)
SPEC = replace(
    COMPACT_SPEC, key="garment_overlap",
    resource_modules=tuple(
        module if module.palu_id == 3 else
        replace(module, quantity=sum(module.palu_id in palus for palus in PALUS))
        for module in COMPACT_SPEC.resource_modules),
    workstations=tuple(replace(ws, palus=palus)
                       for ws, palus in zip(COMPACT_SPEC.workstations, PALUS)),
)
