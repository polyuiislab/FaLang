"""Represent production, holding, and storage roles independently of physical factory bindings."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Iterable, List, Optional, Tuple


class VirtualResourceKind(str, Enum):


    PRODUCTION = "production"   
    HOLDING = "holding"         
    STORAGE = "storage"         


# The declaration keyword distinguishes Buffer and Storage roles even when both use Memory.


_DECL_KIND_TO_ROLE = {
    "Resource": VirtualResourceKind.PRODUCTION,
    "Buffer": VirtualResourceKind.HOLDING,
    "Storage": VirtualResourceKind.STORAGE,
}


_LOGICAL_CAPABILITY_ROLES = {
    "Process": {VirtualResourceKind.PRODUCTION},
    "Inspect": {VirtualResourceKind.PRODUCTION},
    "Adjust": {VirtualResourceKind.PRODUCTION},
    "Move": {VirtualResourceKind.PRODUCTION, VirtualResourceKind.HOLDING},
    "Load": {VirtualResourceKind.STORAGE, VirtualResourceKind.HOLDING},
    "Store": {VirtualResourceKind.STORAGE, VirtualResourceKind.HOLDING},
}


class VirtualResourceError(Exception):
    """A logical resource-role declaration is invalid."""


@dataclass(frozen=True)
class VirtualResource:


    name: str
    kind: VirtualResourceKind
    capabilities: Tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover
        caps = ",".join(self.capabilities)
        return f"{self.kind.value}:{self.name}<{caps}>"


@dataclass(frozen=True)
class VirtualMaterialAddress:
    """A logical WIP holding address in the virtual material address space."""

    name: str

    def __str__(self) -> str:  # pragma: no cover
        return f"VMAS({self.name})"


@dataclass
class VirtualResourceSpace:


    resources: Dict[str, VirtualResource] = field(default_factory=dict)
    materials: Tuple[str, ...] = ()
    # Record source physical attributes; target-model capacities govern physical allocation.

    physical_attributes: Dict[str, Dict[str, object]] = field(default_factory=dict)


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

                space.physical_attributes[name] = {"capacity": capacity}

        space.materials = tuple(material_names)
        return space

    @staticmethod
    def _validate_capabilities(name: str, kind: VirtualResourceKind, capabilities: Iterable[str]) -> None:
        for cap in capabilities:
            allowed = _LOGICAL_CAPABILITY_ROLES.get(cap)
            if allowed is None:
                # Domain capability names are resolved against the factory during target lowering.

                continue
            if kind not in allowed:
                raise VirtualResourceError(
                    f"Logical capability <{cap}> cannot be declared on {kind.value} role {name!r}"
                )


    def production_roles(self) -> List[VirtualResource]:
        return [r for r in self.resources.values() if r.kind is VirtualResourceKind.PRODUCTION]

    def holding_roles(self) -> List[VirtualResource]:
        return [r for r in self.resources.values() if r.kind is VirtualResourceKind.HOLDING]

    def storage_roles(self) -> List[VirtualResource]:
        return [r for r in self.resources.values() if r.kind is VirtualResourceKind.STORAGE]

    def material_address_space(self) -> List[VirtualMaterialAddress]:
        """Return the logical holding addresses required by the program."""
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

    cfg.vmrs = space
    cfg.vmas = [addr.name for addr in space.material_address_space()]
