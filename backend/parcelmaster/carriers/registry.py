"""Finds carrier modules by walking the ``carriers`` package."""

from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path

from .. import carriers as carriers_package
from .base import CarrierModule


class Registry:
    def __init__(self, modules: list[CarrierModule], locales: dict[str, dict[str, str]]) -> None:
        self._modules = {module.id: module for module in modules}
        #: language -> flat keys ("carrier.<id>.<...>") -> text
        self.locales = locales

    def all(self) -> list[CarrierModule]:
        return sorted(self._modules.values(), key=lambda m: (m.country == "", m.country, m.name))

    def get(self, module_id: str) -> CarrierModule | None:
        return self._modules.get(module_id)


def _flatten(prefix: str, node: object, out: dict[str, str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            _flatten(f"{prefix}.{key}" if prefix else key, value, out)
    elif isinstance(node, str):
        out[prefix] = node


def discover() -> Registry:
    modules: list[CarrierModule] = []
    locales: dict[str, dict[str, str]] = {}
    prefix = carriers_package.__name__ + "."
    for info in pkgutil.walk_packages(carriers_package.__path__, prefix):
        if not info.ispkg:
            continue
        package = importlib.import_module(info.name)
        module = getattr(package, "MODULE", None)
        if not isinstance(module, CarrierModule):
            continue
        if not module.id:
            raise ValueError(f"{info.name}: carrier module has no id")
        if any(existing.id == module.id for existing in modules):
            raise ValueError(f"duplicate carrier module id {module.id!r}")
        modules.append(module)
        locale_dir = Path(package.__file__).parent / "locales"
        for path in sorted(locale_dir.glob("*.json")):
            texts: dict[str, str] = {}
            _flatten(f"carrier.{module.id}", json.loads(path.read_text("utf-8")), texts)
            locales.setdefault(path.stem, {}).update(texts)
    return Registry(modules, locales)
