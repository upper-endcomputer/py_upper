"""
Observable property system for MVVM
支持属性变化通知和数据绑定
"""

from typing import Any, Callable, Optional, List, TypeVar, Generic


T = TypeVar('T')


class PropertyChangedEvent:
    """属性变化事件"""
    
    def __init__(self, property_name: str, old_value: Any, new_value: Any):
        self.property_name = property_name
        self.old_value = old_value
        self.new_value = new_value
    
    def __repr__(self) -> str:
        return f"PropertyChangedEvent({self.property_name}: {self.old_value} -> {self.new_value})"


class ObservableProperty(Generic[T]):
    """可观察属性 - 支持数据绑定和变化通知"""
    
    def __init__(self, default_value: Optional[T] = None):
        self._value: Optional[T] = default_value
        self._listeners: List[Callable[[PropertyChangedEvent], None]] = []
        self._property_name: Optional[str] = None
    
    def set_property_name(self, name: str) -> None:
        """设置属性名称（由ViewModel自动调用）"""
        self._property_name = name
    
    def get_value(self) -> Optional[T]:
        """获取属性值"""
        return self._value
    
    def set_value(self, value: Optional[T]) -> bool:
        """设置属性值，返回是否发生了变化"""
        if self._value != value:
            old_value = self._value
            self._value = value
            self._notify_changed(old_value, value)
            return True
        return False
    
    def _notify_changed(self, old_value: Any, new_value: Any) -> None:
        """通知所有监听器属性已变化"""
        event = PropertyChangedEvent(
            property_name=self._property_name or "unknown",
            old_value=old_value,
            new_value=new_value
        )
        for listener in self._listeners:
            try:
                listener(event)
            except Exception as e:
                print(f"Error in property changed listener: {e}")
    
    def subscribe(self, callback: Callable[[PropertyChangedEvent], None]) -> Callable[[], None]:
        """
        订阅属性变化事件
        返回一个unsubscribe函数
        """
        self._listeners.append(callback)
        return lambda: self._listeners.remove(callback) if callback in self._listeners else None
    
    def __get__(self, obj: Any, objtype: Optional[type] = None) -> Optional[T]:
        """Descriptor get"""
        if obj is None:
            return self
        return self._value
    
    def __set__(self, obj: Any, value: Optional[T]) -> None:
        """Descriptor set"""
        self.set_value(value)

