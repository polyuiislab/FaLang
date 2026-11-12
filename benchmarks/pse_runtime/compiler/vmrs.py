# F_IDE_Project/compiler/vmrs.py
"""
Paper 1 Method 1 —— Virtual Manufacturing Resource Space (VMRS) / Virtual Material Address Space (VMAS)。

制造语境：一张定制订单只应该说明"这一步需要什么能力"、"物料中间需要在哪里被
暂存"，而不应该指定具体是哪台机台、哪个线边库位。FaLang 源码里的 resource /
buffer / storage 因此都是**逻辑角色**：

- production role：声明"需要一个具备某能力的加工资源"，例如 `Resource Assembly_PM: Device<Process, Adjust>`；
- holding role：声明"需要一个 WIP 暂存位置"，例如 `Buffer IntermediateBuffer`。这些逻辑暂存位置的集合就是 VMAS；
- storage role：声明"需要一个大容量存储位置"，例如 `Storage Warehouse`。

`Buffer IntermediateBuffer` 不等于任何 `Buffer_07`。从逻辑角色到 `F_t` 中具体持有位置的
绑定，由 `compiler/target_lowering.py` 在 Factory Target Machine Model 上完成。

本模块只做分类、逻辑语义校验与记录，**不引入任何物理 ID、物理容量或目标机信息**。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple


class VirtualResourceKind(str, Enum):
    """逻辑资源角色的类别。"""

    PRODUCTION = "production"   # 具备能力的加工/物流资源角色
    HOLDING = "holding"         # WIP 暂存位置角色（VMAS 成员）
    STORAGE = "storage"         # 大容量存储位置角色


# 声明关键字到逻辑角色的映射。源码关键字是唯一权威来源：
# 同一个 `Memory` 类别既可能出现在 `Buffer`（暂存）也可能出现在 `Storage`（仓储），
# 因此不能用 category 推断角色。
_DECL_KIND_TO_ROLE = {
    "Resource": VirtualResourceKind.PRODUCTION,
    "Buffer": VirtualResourceKind.HOLDING,
    "Storage": VirtualResourceKind.STORAGE,
}

# 逻辑能力名（源码 `<Process, Adjust>` 中写的名字）到允许的声明角色的白名单。
# 这里的约束是纯逻辑的：它不查目标机模型，只拒绝把能力注在错误的角色上。
_LOGICAL_CAPABILITY_ROLES = {
    "Process": {VirtualResourceKind.PRODUCTION},
    "Inspect": {VirtualResourceKind.PRODUCTION},
    "Adjust": {VirtualResourceKind.PRODUCTION},
    "Move": {VirtualResourceKind.PRODUCTION, VirtualResourceKind.HOLDING},
    "Load": {VirtualResourceKind.STORAGE, VirtualResourceKind.HOLDING},
    "Store": {VirtualResourceKind.STORAGE, VirtualResourceKind.HOLDING},
}


class VirtualResourceError(Exception):
    """逻辑资源角色声明不合法。"""


@dataclass(frozen=True)
class VirtualResource:
    """一个逻辑资源角色。只带逻辑语义，不带物理属性。"""

    name: str
    kind: VirtualResourceKind
    capabilities: Tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        caps = ",".join(self.capabilities)
        return f"{self.kind.value}:{self.name}<{caps}>"


@dataclass(frozen=True)
class VirtualMaterialAddress:
    """VMAS 成员：一个逻辑 WIP 持有位置。"""

    name: str

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        return f"VMAS({self.name})"


@dataclass
class VirtualResourceSpace:
    """一次编译中出现的全部逻辑资源角色，以及被拒绝的物理属性。"""

    resources: Dict[str, VirtualResource] = field(default_factory=dict)
    materials: Tuple[str, ...] = ()
    # 源码中出现的物理属性（例如 `Buffer B: Memory<> <Capacity=2>`）。
    # Paper 1 只记录、不采用：物理容量交由 Factory Target Machine Model 决定。
    physical_attributes: Dict[str, Dict[str, object]] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    @classmethod
    def from_ast(cls, ast) -> "VirtualResourceSpace":
        space = cls()
        material_names: List[str] = []

        for decl in getattr(ast, "declarations", []) or []:
            decl_type = type(decl).__name__
            if decl_type == "MaterialDecl":
                name = getattr(decl, "name", None)
                if isinstance(name, str) and name:
                    material_names.append(name)
                continue
            if decl_type != "ResourceDecl":
                continue

            name = getattr(decl, "name", None)
            if not isinstance(name, str) or not name:
                continue
            decl_kind = getattr(decl, "decl_kind", None) or "Resource"
            kind = _DECL_KIND_TO_ROLE.get(decl_kind)
            if kind is None:
                raise VirtualResourceError(
                    f"Unknown resource declaration keyword: {decl_kind!r} for {name!r}"
                )
            capabilities = tuple(getattr(decl, "capabilities", None) or ())
            space._validate_capabilities(name, kind, capabilities)

            if name in space.resources:
                raise VirtualResourceError(f"Duplicate logical resource role: {name}")
            space.resources[name] = VirtualResource(name=name, kind=kind, capabilities=capabilities)

            capacity = getattr(decl, "capacity", None)
            if capacity is not None:
                # 物理容量属于 Factory Target Machine Model，不属于订单意图。
                space.physical_attributes[name] = {"capacity": capacity}

        space.materials = tuple(material_names)
        return space

    @staticmethod
    def _validate_capabilities(name: str, kind: VirtualResourceKind, capabilities: Iterable[str]) -> None:
        for cap in capabilities:
            allowed = _LOGICAL_CAPABILITY_ROLES.get(cap)
            if allowed is None:
                # 领域能力（例如 `Assembly`、`Welding`）不是语法关键字，允许作为能力名出现，
                # 其可用性由 Target Lowering 对照 F_t 判定。
                continue
            if kind not in allowed:
                raise VirtualResourceError(
                    f"Logical capability <{cap}> cannot be declared on {kind.value} role {name!r}"
                )

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def production_roles(self) -> List[VirtualResource]:
        return [r for r in self.resources.values() if r.kind is VirtualResourceKind.PRODUCTION]

    def holding_roles(self) -> List[VirtualResource]:
        return [r for r in self.resources.values() if r.kind is VirtualResourceKind.HOLDING]

    def storage_roles(self) -> List[VirtualResource]:
        return [r for r in self.resources.values() if r.kind is VirtualResourceKind.STORAGE]

    def material_address_space(self) -> List[VirtualMaterialAddress]:
        """VMAS：本程序要求的全部逻辑 WIP 持有位置。"""
        return [VirtualMaterialAddress(r.name) for r in self.holding_roles()]

    def kind_of(self, name: str) -> Optional[VirtualResourceKind]:
        role = self.resources.get(name)
        return role.kind if role else None

    def capabilities_of(self, name: str) -> Tuple[str, ...]:
        role = self.resources.get(name)
        return role.capabilities if role else ()

    def is_known(self, name: str) -> bool:
        return name in self.resources

    def summary(self) -> str:
        return (
            f"VMRS(production={len(self.production_roles())}, "
            f"vmas={len(self.holding_roles())}, storage={len(self.storage_roles())}, "
            f"physical_attrs_dropped={len(self.physical_attributes)})"
        )


def attach_to_cfg(cfg, space: VirtualResourceSpace) -> None:
    """把 VMRS 挂到 CFG 上，供 Target Lowering 与后续阶段读取。"""
    cfg.vmrs = space
    cfg.vmas = [addr.name for addr in space.material_address_space()]
