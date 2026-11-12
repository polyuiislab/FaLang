"""Study 3：含跨工段复合单元的服装线，每个线边库位容量为 2。

两台裁缝复合单元、两台缝检复合单元与一台检包单元支持原位续加工。
专用工位仍可分担负载；所有选择器使用同一设备和工艺路线。
"""
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
