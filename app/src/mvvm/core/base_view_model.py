"""
Base ViewModel for MVVM
所有ViewModel都应继承此类
"""

from typing import Any, Callable, Dict, Optional
from mvvm.core.observable import ObservableProperty, PropertyChangedEvent


class BaseViewModel:
    """
    ViewModel基类
    提供属性通知、命令管理、数据绑定等功能
    """
    
    def __init__(self):
        self._properties: Dict[str, ObservableProperty] = {}
        self._property_changed_listeners: list = []
        self._initialize_properties()
    
    def _initialize_properties(self) -> None:
        """
        初始化所有属性
        子类应该重写此方法来初始化自己的属性
        """
        pass
    
    def _register_property(self, name: str, property_obj: ObservableProperty) -> None:
        """注册一个属性并设置其名称"""
        property_obj.set_property_name(name)
        self._properties[name] = property_obj
        # 订阅属性变化事件
        property_obj.subscribe(self._on_property_changed)
    
    def get_property(self, name: str) -> Optional[ObservableProperty]:
        """获取指定名称的属性"""
        return self._properties.get(name)
    
    def get_property_value(self, name: str) -> Any:
        """获取属性值"""
        prop = self._properties.get(name)
        return prop.get_value() if prop else None
    
    def set_property_value(self, name: str, value: Any) -> bool:
        """设置属性值"""
        prop = self._properties.get(name)
        if prop:
            return prop.set_value(value)
        return False
    
    def _on_property_changed(self, event: PropertyChangedEvent) -> None:
        """
        属性变化回调
        将在任何属性值改变时被调用
        """
        # 通知所有监听器
        for listener in self._property_changed_listeners:
            try:
                listener(event)
            except Exception as e:
                print(f"Error in property changed listener: {e}")
        
        # 调用虚方法，便于子类处理特定的属性变化
        self.on_property_changed(event)
    
    def on_property_changed(self, event: PropertyChangedEvent) -> None:
        """
        属性变化虚方法
        子类可以重写此方法来处理特定的属性变化
        """
        pass
    
    def subscribe_property_changed(self, callback: Callable[[PropertyChangedEvent], None]) -> Callable[[], None]:
        """
        订阅属性变化事件
        返回unsubscribe函数
        """
        self._property_changed_listeners.append(callback)
        return lambda: self._property_changed_listeners.remove(callback) if callback in self._property_changed_listeners else None
    
    def dispose(self) -> None:
        """清理资源"""
        self._properties.clear()
        self._property_changed_listeners.clear()
    
    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

