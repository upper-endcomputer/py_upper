# 中文维护说明：集中创建可观察属性与 ViewModel；重导出必须保持同一类对象身份。
# 注释规范：docs/COMMENTING_GUIDE.md；修改行为时同步更新边界回归。
"""Factories: property factory for declarative ViewModel fields and the
ViewModelFactory that centralizes construction."""
from __future__ import annotations
from typing import Any, Type

from mvvm.core.observable import ObservableProperty


class PropertyFactory:
    """Create named ObservableProperty descriptors in one call."""

    @staticmethod
    def create(default: Any = None, name: str | None = None) -> ObservableProperty:
        """创建可观察属性并按需赋名，供 ViewModel 注册使用。"""
        prop: ObservableProperty = ObservableProperty(default)
        if name:
            prop.set_property_name(name)
        return prop


class ViewModelFactory:
    """Instantiate ViewModels with shared construction logic."""

    _registry: dict[str, Type] = {}

    @classmethod
    def register(cls, name: str, view_model_type: Type) -> None:
        """以名称登记 ViewModel 类型；同名注册替换先前类型。"""
        cls._registry[name] = view_model_type

    @classmethod
    def create(cls, name: str, **kwargs) -> Any:
        """按注册名实例化 ViewModel 并转交构造参数；未知名称抛出 KeyError。"""
        vm_type = cls._registry.get(name)
        if vm_type is None:
            raise KeyError(f"ViewModel '{name}' is not registered")
        return vm_type(**kwargs)
