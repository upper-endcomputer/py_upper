"""MVVM Application Container
协调 Model、View 和 ViewModel 的交互
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Type

from mvvm.core import BaseViewModel
from mvvm.models import ApplicationModel
from mvvm.views import ViewBase


class ServiceLocator:
    """服务定位器 - 实现依赖注入

    每个 MVVMApplication 一个实例：类级注册表会让一个应用取到另一个应用的服务，
    正是容器要挡掉的那种隐式耦合。
    """

    def __init__(self) -> None:
        self._services: Dict[str, Any] = {}

    def register_service(self, service_name: str, service_instance: Any) -> None:
        """注册服务"""
        self._services[service_name] = service_instance

    def get_service(self, service_name: str) -> Optional[Any]:
        """获取已注册的服务"""
        return self._services.get(service_name)

    def resolve(self, service_type: Type) -> Optional[Any]:
        """按类型解析服务：返回第一个 isinstance 命中的实例"""
        for service in self._services.values():
            if isinstance(service, service_type):
                return service
        return None


class MVVMApplication:
    """MVVM 应用容器：持有 root model、ViewModel/View 注册表与生命周期"""

    def __init__(self, model: Optional[ApplicationModel] = None) -> None:
        self.model = model or ApplicationModel()
        self.viewmodels: Dict[str, BaseViewModel] = {}
        self.views: Dict[str, ViewBase] = {}
        self.service_locator = ServiceLocator()
        self._is_running = False

    # --- ViewModel 注册表 -------------------------------------------------
    def register_viewmodel(self, name: str, viewmodel: BaseViewModel) -> None:
        """按名字注册 ViewModel"""
        self.viewmodels[name] = viewmodel

    def get_viewmodel(self, name: str) -> Optional[BaseViewModel]:
        """获取已注册的 ViewModel"""
        return self.viewmodels.get(name)

    # --- View 注册表 ------------------------------------------------------
    def register_view(self, name: str, view: ViewBase) -> None:
        """按名字注册 View"""
        self.views[name] = view

    def get_view(self, name: str) -> Optional[ViewBase]:
        """获取已注册的 View"""
        return self.views.get(name)

    # --- 服务 -------------------------------------------------------------
    def register_service(self, service_name: str, service_instance: Any) -> None:
        """注册服务到服务定位器"""
        self.service_locator.register_service(service_name, service_instance)

    def get_service(self, service_name: str) -> Optional[Any]:
        """按名字获取服务"""
        return self.service_locator.get_service(service_name)

    def resolve_service(self, service_type: Type) -> Optional[Any]:
        """按类型获取服务"""
        return self.service_locator.resolve(service_type)

    # --- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        """启动应用：置位运行标记并显示所有已注册的 View"""
        self._is_running = True
        self.model.is_running = True
        for view in self.views.values():
            view.show()

    def stop(self) -> None:
        """停止应用并释放视图与 ViewModel"""
        self._is_running = False
        self.model.is_running = False
        self._cleanup()

    def _cleanup(self) -> None:
        for view in self.views.values():
            view.close()
        for viewmodel in self.viewmodels.values():
            viewmodel.dispose()
        self.views.clear()
        self.viewmodels.clear()

    @property
    def is_running(self) -> bool:
        """应用是否处于运行状态"""
        return self._is_running

    def __repr__(self) -> str:
        return f"MVVMApplication(viewmodels={len(self.viewmodels)}, views={len(self.views)})"
