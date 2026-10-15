"""Factories: property factory for declarative ViewModel fields and the
ViewModelFactory that centralizes construction."""
from __future__ import annotations
from typing import Any, Type

from mvvm.core.observable import ObservableProperty


class PropertyFactory:
    """Create named ObservableProperty descriptors in one call."""

    @staticmethod
    def create(default: Any = None, name: str | None = None) -> ObservableProperty:
        prop: ObservableProperty = ObservableProperty(default)
        if name:
            prop.set_property_name(name)
        return prop


class ViewModelFactory:
    """Instantiate ViewModels with shared construction logic."""

    _registry: dict[str, Type] = {}

    @classmethod
    def register(cls, name: str, view_model_type: Type) -> None:
        cls._registry[name] = view_model_type

    @classmethod
    def create(cls, name: str, **kwargs) -> Any:
        vm_type = cls._registry.get(name)
        if vm_type is None:
            raise KeyError(f"ViewModel '{name}' is not registered")
        return vm_type(**kwargs)
